"""Route-level tests for OSS proxy downloads; no bucket or credentials required."""
import gzip
import io
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import requests
from oss2.http import Response as OssResponse
from oss2.exceptions import RequestError, ServerError
from oss2.models import GetObjectResult, HeadObjectResult
from urllib3.response import HTTPResponse
from werkzeug.http import parse_options_header, parse_range_header

import oss_store
import web


HASH = "ab" * 32
FILE_URL = "/file/get_file/" + HASH
DATA = bytes(range(256)) * 8192


class ProxyDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = Mock()
        cls.files.file_exists.return_value = True
        cls.files.get_media.return_value = {
            "status": "done", "thumb_is_original": False, "thumb_size": 256,
        }
        cls.stickers = Mock()
        cls.stickers.get_sticker_file_info.return_value = {"file_name": "sticker.gif"}
        config_reader = patch.object(web, "read_json", return_value={})
        config_reader.start()
        cls.addClassCleanup(config_reader.stop)
        with patch.object(web.threading.Thread, "start"):
            cls.app = web.main(
                0, 0, None, None, None, None, None, cls.files, None, None,
                Mock(), None, sticker_cursor=cls.stickers, jwt_secret=b"test", jwt_key_store=Mock(),
            )
        cls.app.testing = True

    def setUp(self):
        self.client = self.app.test_client()
        self.data = DATA
        self.content_encoding = None
        self.body = None
        self.requested_ranges = []
        self.files.get_metadata.return_value = {"file_name": "资料.pdf", "size": len(DATA)}
        self.bucket = Mock()
        self.bucket.head_object.side_effect = lambda key, **kw: self.result(head=True)
        self.bucket.get_object.side_effect = self.get_object
        config = {
            "storage_backend": "oss2", "file_download_mode": "proxy",
            "oss2_authid": "id", "oss2_authkey": "key",
            "oss2_endpoint": "https://example.invalid", "oss2_bucket": "test",
        }
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(oss_store, "_read_config", return_value=config))
        patches.enter_context(patch.object(oss_store, "_get_bucket", return_value=self.bucket))
        patches.enter_context(patch.object(web.file, "ensure_media"))

    def result(self, head=False, interval=None):
        data = self.data
        response = requests.Response()
        response.status_code = 200
        response.headers.update({
            "ETag": '"test-etag"', "Last-Modified": "Fri, 27 Nov 2026 10:00:00 GMT",
        })
        if self.content_encoding:
            response.headers["Content-Encoding"] = self.content_encoding
        if interval:
            first, last = interval
            response.status_code = 206
            response.headers["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
            data = data[first:last + 1]
        response.headers["Content-Length"] = str(len(data))
        body = io.BytesIO(b"" if head else data)
        response.raw = HTTPResponse(body=body, headers=response.headers, preload_content=False)
        if head:
            return HeadObjectResult(OssResponse(response))
        self.body = body
        return GetObjectResult(OssResponse(response))

    def get_object(self, key, byte_range=None, headers=None, **kwargs):
        if byte_range is None and headers and "Range" in headers:
            byte_range = parse_range_header(headers["Range"]).range_for_length(len(self.data))
            byte_range = (byte_range[0], byte_range[1] - 1)
        self.requested_ranges.append(byte_range)
        return self.result(interval=byte_range)

    def test_first_chunk_precedes_end_of_origin_body_and_close_releases_it(self):
        with self.client.get(FILE_URL, buffered=False) as reply:
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.content_length, len(DATA))
            prefix = next(iter(reply.response))
            self.assertTrue(prefix)
            self.assertEqual(prefix, DATA[:len(prefix)])
            self.assertLess(self.body.tell(), len(DATA))
            self.assertFalse(self.body.closed)
        self.assertTrue(self.body.closed)

    def test_close_before_first_iteration(self):
        with self.app.test_request_context(FILE_URL):
            reply = self.app.full_dispatch_request()
            try:
                self.assertEqual(reply.status_code, 200)
            finally:
                reply.close()
        self.assertTrue(self.body is None or self.body.closed)

    def test_head_and_conditional_get_do_not_read_body(self):
        head = self.client.head(FILE_URL)
        self.assertEqual((head.status_code, head.data, head.content_length), (200, b"", len(DATA)))
        self.assertEqual(head.cache_control.max_age, web.FILE_CACHE_MAX_AGE)
        self.assertTrue(head.cache_control.public)
        disposition, options = parse_options_header(head.headers["Content-Disposition"])
        self.assertEqual((disposition, options["filename"]), ("attachment", "资料.pdf"))
        self.assertEqual(head.mimetype, "application/pdf")
        cached = self.client.get(FILE_URL, headers={"If-None-Match": head.headers["ETag"]})
        self.assertEqual((cached.status_code, cached.data), (304, b""))
        self.bucket.get_object.assert_not_called()

    def test_resume_and_suffix_read_only_the_requested_bytes(self):
        for value, first, last in (("2-12", 2, 12), ("-7", len(DATA) - 7, len(DATA) - 1)):
            with self.subTest(value=value), self.client.get(FILE_URL, headers={"Range": "bytes=" + value}) as reply:
                self.assertEqual(reply.status_code, 206)
                self.assertEqual(reply.headers["Content-Range"], f"bytes {first}-{last}/{len(DATA)}")
                self.assertEqual(reply.content_length, last - first + 1)
                self.assertEqual(reply.data, DATA[first:last + 1])
                self.assertEqual(self.requested_ranges.pop(), (first, last))

    def test_unsatisfiable_range(self):
        with self.client.get(FILE_URL, headers={"Range": f"bytes={len(DATA)}-"}) as reply:
            self.assertEqual(reply.status_code, 416)
            self.assertEqual(reply.headers["Content-Range"], f"bytes */{len(DATA)}")
        self.bucket.get_object.assert_not_called()

    def test_empty_object(self):
        self.data = b""
        with self.client.get(FILE_URL) as reply:
            self.assertEqual((reply.status_code, reply.content_length, reply.data), (200, 0, b""))
        with self.client.get(FILE_URL, headers={"Range": "bytes=0-"}) as reply:
            self.assertEqual((reply.status_code, reply.headers["Content-Range"]), (416, "bytes */0"))

    def test_redirect_mode_and_proxy_fallback(self):
        self.data = b"fixture"
        url = "https://example.invalid/signed"
        with patch.object(oss_store, "get_download_mode", return_value="redirect"), patch.object(
            oss_store, "presigned_download_url", side_effect=[url, ""],
        ):
            with self.client.get(FILE_URL) as reply:
                self.assertEqual((reply.status_code, reply.location), (307, url))
                self.bucket.get_object.assert_not_called()
            with self.client.get(FILE_URL) as reply:
                self.assertEqual((reply.status_code, reply.data), (200, self.data))

    def test_if_range_mismatch_returns_entire_file(self):
        self.data = b"abcdefgh"
        with self.client.get(FILE_URL, headers={"Range": "bytes=2-5", "If-Range": '"older"'}) as reply:
            self.assertEqual((reply.status_code, reply.data), (200, self.data))
            self.assertNotIn("Content-Range", reply.headers)

    def test_validators_are_evaluated_before_range(self):
        for headers, status in (
            ({"If-Match": "*"}, 200),
            ({"If-Match": '"stale"'}, 412),
            ({"If-Match": '"test-etag"', "If-None-Match": 'W/"test-etag"', "Range": "bytes=1-3"}, 304),
            ({"If-Unmodified-Since": "Thu, 26 Nov 2026 10:00:00 GMT"}, 412),
            ({"If-Modified-Since": "Fri, 27 Nov 2026 10:00:00 GMT"}, 304),
        ):
            with self.subTest(headers=headers), self.client.head(FILE_URL, headers=headers) as reply:
                self.assertEqual(reply.status_code, status)
        self.bucket.get_object.assert_not_called()

    def test_upstream_errors(self):
        for error, status in (
            (ServerError(404, {}, b"", {}), 404),
            (RequestError(Exception("connection failed")), 502),
        ):
            self.bucket.get_object.side_effect = error
            with self.subTest(status=status), self.client.get(FILE_URL) as reply:
                self.assertEqual(reply.status_code, status)

    def test_ignored_origin_range_is_closed(self):
        self.bucket.get_object.side_effect = lambda *args, **kwargs: self.result()
        with self.client.get(FILE_URL, headers={"Range": "bytes=8-15"}) as reply:
            self.assertEqual(reply.status_code, 502)
            self.assertTrue(self.body.closed)

    def test_content_encoding_matches_transmitted_bytes(self):
        self.data = gzip.compress(b"example" * 8192)
        self.content_encoding = "gzip"
        with self.client.get(FILE_URL) as reply:
            self.assertEqual(reply.headers["Content-Encoding"], "gzip")
            self.assertEqual(reply.content_length, len(self.data))
            self.assertEqual(reply.data, self.data)

    def test_thumbnail_and_sticker_disposition(self):
        self.data = b"fixture"
        for path, kind, disposition, mimetype in (
            ("/file/get_thumbnail/", "thumb", "inline", "image/webp"),
            ("/sticker/get/", "sticker", "attachment", "image/gif"),
        ):
            with self.subTest(kind=kind), self.client.get(path + HASH) as reply:
                self.assertEqual((reply.status_code, reply.data), (200, self.data))
                self.assertEqual(reply.mimetype, mimetype)
                self.assertEqual(parse_options_header(reply.headers["Content-Disposition"])[0], disposition)
                self.assertEqual(self.bucket.get_object.call_args.args[0], oss_store.object_key(kind, HASH))


if __name__ == "__main__":
    unittest.main()
