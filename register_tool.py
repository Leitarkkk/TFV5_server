"""
没错，只是为了放验证码和邮箱验证的程序
"""

import os
from concurrent.futures import ThreadPoolExecutor
import secrets
import smtplib
from string import ascii_letters, digits
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from threading import BoundedSemaphore
from time import time

from json_store import read_json, update_json

MAX_DELAY = 300
EMAIL_CODE_LIFETIME = 15 * 60
ACTIVATION = "activation"
PASSWORD_RESET = "password_reset"

def login_email(sender_email : str, password : str, smtp_host=None, smtp_port=None, smtp_use_ssl=True):
    """
    password 是登录密码或者授权码，视邮件提供商要求
    smtp_host/smtp_port/smtp_use_ssl 可手动指定 SMTP 参数；
    未指定 smtp_host 时按邮箱域名自动猜测 smtp.<domain>
    smtp_use_ssl: True = SMTP_SSL 直连（默认 465），False = SMTP + STARTTLS（默认 587）
    """
    if smtp_host:
        smtp_server = smtp_host
    else:
        smtp_server = 'smtp.' + sender_email.split('@')[1]
    if smtp_port is None:
        smtp_port = 465 if smtp_use_ssl else 587
    smtp_class = smtplib.SMTP_SSL if smtp_use_ssl else smtplib.SMTP
    server = smtp_class(smtp_server, smtp_port, timeout=10)
    try:
        if not smtp_use_ssl:
            server.starttls()
        server.login(sender_email, password)
        return server
    except Exception:
        server.close()
        raise

def send_email(server, sender_email : str, receiver_email : str, subject : str, content : str):
    """
    server 是 smtplib.SMTP_SSL 对象
    """
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = Header(subject, 'utf-8')
    msg.attach(MIMEText(content, "plain", "utf-8"))
    try:
        server.sendmail(sender_email, receiver_email, msg.as_string())
    except (smtplib.SMTPException, OSError):
        return False
    return True

def _captcha_record(stamp, value):
    if isinstance(value, dict):
        return value["answer"], value["expires_at"]
    # Older images use the Unix timestamp as their stamp and store only the answer.
    return value, int(stamp) + MAX_DELAY


def _remove_legacy_image(folder_path, stamp):
    try:
        os.remove(os.path.join(folder_path, "{}.png".format(int(stamp))))
    except (OSError, ValueError):
        pass

def generate_captcha(port_api : int, Imgcaptcha, lock):
    """Return an independent integer stamp and the generated PNG."""
    folder_path = "res/{}/captcha".format(port_api)
    captcha_text = ''.join(secrets.choice(ascii_letters + digits) for _ in range(6))
    with Imgcaptcha.generate(captcha_text) as image:
        png = image.read()

    def issue(challenges):
        now = time()
        for stamp, value in list(challenges.items()):
            _, expires_at = _captcha_record(stamp, value)
            if now >= expires_at:
                del challenges[stamp]
                if isinstance(value, str):
                    _remove_legacy_image(folder_path, stamp)
        # Keep the opaque ID within JavaScript's safe integer range.
        while True:
            stamp = (1 << 52) + secrets.randbelow(1 << 52)
            if str(stamp) not in challenges:
                break
        challenges[str(stamp)] = {"answer": captcha_text, "expires_at": now + MAX_DELAY}
        return stamp

    with lock:
        stamp = update_json(os.path.join(folder_path, "captcha.json"), issue)
    return stamp, png

def verify_captcha(port_api : int, time_stamp : int | str, verify_text : str, lock):
    """
    校验验证码（校验一次之后就报废啦）
    """
    if (
        isinstance(time_stamp, str) and len(time_stamp) <= 16
        and time_stamp.isascii() and time_stamp.isdecimal()
    ):
        time_stamp = int(time_stamp)
    if type(time_stamp) is not int or not 0 <= time_stamp < (1 << 53) or not isinstance(verify_text, str):
        return False, False
    stamp = str(time_stamp)
    folder_path = "res/{}/captcha".format(port_api)
    path = os.path.join(folder_path, "captcha.json")

    def verify(challenges):
        value = challenges.pop(stamp, None)
        if value is None:
            return False, False
        answer, expires_at = _captcha_record(stamp, value)
        if isinstance(value, str):
            _remove_legacy_image(folder_path, stamp)
        if time() >= expires_at:
            return False, False
        return answer.lower() == verify_text.lower(), True

    with lock:
        if stamp not in read_json(path):
            return False, False
        return update_json(path, verify)

def issue_email_code(port_api : int, email : str, activate_lock, *, uid: int, purpose: str):
    """生成验证码并同步写入 activate.json，返回验证码 必须在请求返回前完成"""
    def issue(codes):
        now = time()
        for address, entries in list(codes.items()):
            # Integer entries were written by older versions.
            if isinstance(entries, dict):
                fresh = {key: entry for key, entry in entries.items() if now < entry["expires_at"]}
                if fresh:
                    codes[address] = fresh
                else:
                    del codes[address]
        entries = codes.get(email)
        if not isinstance(entries, dict):
            entries = {}
            codes[email] = entries
        current = entries.get(purpose)
        if current and current["uid"] == uid:
            # Resends can arrive out of order, so keep the current valid code.
            return current["code"]
        used_codes = {entry["code"] for entry in entries.values()}
        while True:
            verify_code = 100000 + secrets.randbelow(900000)
            if verify_code not in used_codes:
                break
        entries[purpose] = {"code": verify_code, "uid": uid, "expires_at": now + EMAIL_CODE_LIFETIME}
        return verify_code

    with activate_lock:
        return update_json("res/{}/activate.json".format(port_api), issue)


def send_email_code(sender_email : str, port_api : int, email : str, password : str, config_lock, verify_code : int):
    """发送已生成的验证码"""
    folder_path = "res/{}".format(port_api)
    with config_lock:
        cfg = read_json(folder_path + '/config.json')
        server_name = cfg["server_name"]
        smtp_host = cfg.get("smtp_host", "") or None
        smtp_port = cfg.get("smtp_port")
        smtp_use_ssl = cfg.get("smtp_use_ssl", True)
    session = login_email(sender_email, password, smtp_host, smtp_port, smtp_use_ssl)
    try:
        return send_email(session, sender_email, email, "{} 验证码".format(server_name), "欢迎使用 {}，您的验证码是 {}。".format(server_name, verify_code))
    finally:
        try:
            session.quit()
        except (smtplib.SMTPException, OSError):
            session.close()


# 避免被刷爆 IO
_EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tfv5-smtp")
_EMAIL_PENDING = BoundedSemaphore(32)


def _send_email_code_logged(sender_email, port_api, email, password, config_lock, verify_code):
    try:
        ok = send_email_code(sender_email, port_api, email, password, config_lock, verify_code)
        if not ok:
            print("[WARN] 验证码邮件发送失败: {}".format(email))
    except Exception as e:
        print("[WARN] 验证码邮件发送异常({}): {}".format(email, e))
    finally:
        _EMAIL_PENDING.release()


def email_code_async(sender_email, port_api, email, password, config_lock, activate_lock, *, uid, purpose):
    """
    写入验证码并后台发信
    False 表示排队已满或验证码写入失败
    """
    if not _EMAIL_PENDING.acquire(blocking=False):
        print("[WARN] 验证码邮件队列已满，拒绝本次请求: {}".format(email))
        return False
    try:
        verify_code = issue_email_code(port_api, email, activate_lock, uid=uid, purpose=purpose)
    except Exception as e:
        _EMAIL_PENDING.release()
        print("[WARN] 验证码写入失败({}): {}".format(email, e))
        return False
    try:
        _EMAIL_EXECUTOR.submit(
            _send_email_code_logged,
            sender_email, port_api, email, password, config_lock, verify_code,
        )
    except Exception as e:
        _EMAIL_PENDING.release()
        print("[WARN] 验证码邮件入队失败: {}".format(e))
        return False
    return True

def verify_email(port_api : int, email : str, code : int, lock, *, uid: int, purpose: str, allow_legacy: bool):
    if type(code) is not int or not 100000 <= code <= 999999:
        return False
    folder_path = "res/{}/".format(port_api)
    with lock:
        def verify(code_lst):
            entries = code_lst.get(email)
            if allow_legacy and type(entries) is int and entries == code:
                del code_lst[email]
                return True
            if not isinstance(entries, dict):
                return False
            entry = entries.get(purpose)
            if not entry or entry["uid"] != uid or entry["code"] != code or time() >= entry["expires_at"]:
                return False
            del entries[purpose]
            if not entries:
                del code_lst[email]
            return True
        return update_json(folder_path + '/activate.json', verify)
