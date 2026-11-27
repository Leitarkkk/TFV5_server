# TouchFish V5 Api 文档（搜索）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

搜索接口均为 secret 类型，请求体需按[主文档](main.md)中的 RSA + AES 方式加密，并要求登录。

所有搜索接口都有**按 uid 的内存限流**（默认每 10 秒最多 20 次），超出返回 `{"success": false, "error": "rate_limited"}`（detail_error 模式下为 `RATE_LIMITED`）。
关键词长度不得小于服务器配置 `min_search_length`（默认 2），过短返回 `search_keyword_too_short`（`VALIDATION_SEARCH_KEYWORD_TOO_SHORT`）。

**UID / GID 关键词不受长度限制。**。

---

- `^ POST /user/search` 按用户名搜索用户；`keyword` 为纯数字或 `U<uid>` 时按 UID 精确查找。

请求体：

```json
{
    "keyword" : <keyword>,
    "limit" : <limit>
}
```

`<limit>`（可选，默认 20）为返回上限，最大 20。

返回体（前缀匹配优先，其次按用户名升序）：

```json
[
    {
        "uid" : <uid>,
        "username" : <username>,
        "sign" : <personal_sign>
    }
]
```

- 结果中**不包含 email**。
- 排除封禁用户（`stat == "banned"`）与当前用户自己。
- 匹配为大小写不敏感的模糊匹配，`%` 与 `_` 会被转义为字面量。
- **UID 精确查找**：`keyword` 为纯数字或 `U<uid>`（`u` 也可）时，先按 `uid` 精确匹配，
  命中返回该用户（数组只含一项），**不受 `min_search_length` 限制**。
  - 带 `U` 前缀仅搜索用户
  - 纯数字未命中时会继续按用户名模糊搜索（用户名本身可能就是一串数字）
  - 精确查找**不排除自己**（按 UID 就是定位该账号）
    按用户名模糊搜索则继续排除自己
  - 不搜索封禁

---

- `^ POST /group/search` 按群名搜索群组（分页）；`keyword` 为纯数字或 `G<gid>` 时按 GID 精确查找。

请求体：

```json
{
    "keyword" : <keyword>,
    "limit" : <limit>,
    "offset" : <offset>
}
```

`<offset>`（可选，默认 0）用于分页。

返回体：

```json
{
    "success" : true,
    "groups" : [
        {
            "gid" : <gid>,
            "groupname" : <groupname>,
            "introduction" : <introduction>,
            "member_count" : <member_count>,
            "allow_direct_join" : <bool>,
            "require_review" : <bool>,
            "public_messages" : <bool>,
            "searchable" : <bool>,
            "join_policy" : "open" | "approval" | "invite" | "admin_invite"
        }
    ],
    "has_more" : <bool>
}
```

- **不返回成员/管理员 UID 列表**。
- `has_more` 表示在本次 `offset + limit` 之后是否还有更多结果。
- 只返回 `searchable = true` 的群。设为不可搜索后，群不会出现在任何搜索结果里（含 GID 精确查找），
  但通过 GID 直达的 `/group/preview`、邀请与邀请链接仍可正常访问。
- `join_policy` 语义见 [group.md](group.md) 的 入群机制 ，客户端据此决定展示"加入"/"申请"/"仅邀请"。
- **GID 精确查找**：`keyword` 为纯数字时，服务端先按 `gid` 精确匹配，命中则只返回该群（`has_more = false`），**不受 `min_search_length` 限制**（GID 可能只有 1 位）。仅在 `offset = 0` 时生效。

## 兼容说明

`* GET /group/groupname_search/<groupname>` 为旧版明文接口，**已废弃**，仅为兼容老客户端保留。
新客户端请使用 `POST /group/search`。

## 会话内消息搜索

`POST /message/search` 在**单个会话**内按关键词搜索历史消息（分页、可翻到任意久远位置），
不返回跨会话结果。参数、返回体与权限说明见 [message.md](message.md) 的「搜索会话内消息」。

