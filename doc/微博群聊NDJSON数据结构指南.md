# 📜 微博群聊 NDJSON 数据结构指南

**适用对象**：Python 脚本开发者 / AI 助手
**数据来源**：微博群聊抓取存档 (NDJSON 格式)

### 1. 核心字段定义 (Schema)

每一行 JSON 对象的通用字段读取逻辑：

| 逻辑字段 | 原始 Key (按优先级尝试) | 数据类型 | 说明 |
| --- | --- | --- | --- |
| **UID** | `from_uid` > `uid` > `user_id` | String/Int | 发送者唯一ID |
| **昵称** | `from_user.screen_name` > `sender_nick` | String | 建议优先读取 `from_user` 对象 |
| **头像** | `from_user.profile_image_url` | String | 头像 URL |
| **时间** | `time` > `ts` > `created_at` | Mixed | 可能是 **ISO 字符串** (需处理时区) 或 **13位时间戳** (需 `/1000`) |
| **内容** | `content` > `text` | String | 消息展示文本 |
| **类型** | `type` | Int | **决定消息性质的关键字段** (见下表) |
| **图片** | `pic_infos` | Dict/List | 如果存在，则为图片消息。需检查是否为空 |

### 2. 消息类型对照表 (Type Mapping)

根据实际数据采样验证，请依据 `type` 字段进行分类处理：

| Type ID | 含义 | 详细说明 & 处理建议 |
| --- | --- | --- |
| **321** | **普通消息** | **(占比 90%+)** 用户发送的正常聊天内容。<br>

<br>可能包含纯文本、表情，或者带有 `pic_infos` 的图片。 |
| **100** | **分享/卡片** | 用户将微博分享到群内的卡片消息。<br>

<br>**特征**：内容通常为 "我发布了新微博..."，包含 `url_struct` 链接结构。 |
| **344** | **撤回消息** | **(已失效)** 用户撤回了消息。<br>

<br>**特征**：`content` 字段会被系统重写为 "XXX 撤回了一条消息"。分析时通常应过滤掉。 |
| **322** | **入群通知** | 系统日志。<br>

<br>**特征**：`content` 为 "XXX 加入了群"。包含 `template` 字段。 |
| **323** | **退群通知** | 系统日志。<br>

<br>**特征**：`content` 为 "XXX 退出了群"。 |
| **337** | **管理操作** | 群管理日志。<br>

<br>**特征**：如 "XXX 将 XXX 设为管理员"。 |

### 3. Python 处理代码片段 (Cheat Sheet)

#### (1) 标准化时间戳

由于时间格式混乱，建议使用此函数统一转换为 datetime 对象：

```python
from datetime import datetime, timezone

def parse_time(data):
    # 尝试多个字段
    raw_time = data.get('time') or data.get('ts') or data.get('created_at')
    
    if not raw_time:
        return None
        
    try:
        # 情况1: 13位时间戳 (毫秒) -> 转秒
        if isinstance(raw_time, (int, float)):
            if raw_time > 1e11: 
                raw_time /= 1000
            return datetime.fromtimestamp(raw_time, tz=timezone.utc)
            
        # 情况2: 字符串处理
        if isinstance(raw_time, str):
            # 尝试处理 ISO 格式
            return datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
    except:
        return None

```

#### (2) 提取图片链接

`pic_infos` 结构比较多变，建议使用防御性写法：

```python
def extract_images(data):
    images = []
    pic_infos = data.get('pic_infos')
    
    if not pic_infos:
        return images
        
    # 情况A: 字典格式 {pic_id: {info...}}
    if isinstance(pic_infos, dict):
        for info in pic_infos.values():
            if url := info.get('thumbnail_pic') or info.get('bmiddle_pic'):
                images.append(url)
                
    # 情况B: 列表格式 [{info...}]
    elif isinstance(pic_infos, list):
        for info in pic_infos:
            if url := info.get('thumbnail_pic') or info.get('bmiddle_pic'):
                images.append(url)
                
    return images

```

#### (3) 过滤有效聊天 (排除系统消息)

如果你只想分析用户聊了什么：

```python
def is_valid_chat(data):
    msg_type = data.get('type')
    # 只保留普通消息(321) 和 分享卡片(100)
    # 排除 344(撤回), 322/323(进退群), 337(管理)
    if msg_type in [321, 100]:
        return True
    return False

```