# 知识库AI回答评估工具 - API接口文档

## 基本信息

| 项目 | 说明 |
|------|------|
| 服务地址 | `http://ai.tech.tax.asia.pwcinternal.com:5001` |
| 协议 | HTTP |
| 数据格式 | JSON |
| 编码 | UTF-8 |

---

## 接口列表

### 1. 批量评估接口

向知识库AI提问并对AI回答进行评分，支持批量并发处理。

```
POST /api/evaluate
```

#### 请求头

| 字段 | 值 |
|------|------|
| Content-Type | application/json |

#### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| items | array | 是 | 待评估的问题列表 |
| items[].question | string | 是 | 问题内容 |
| items[].reference_answer | string | 否 | 建议答案（参考答案），不传则只获取AI回答不评分 |
| scoring_prompt | string | 否 | 自定义评分提示词模板，不传则使用服务端已保存的默认模板 |
| thread_count | int | 否 | 并发线程数（1-8），默认 4 |

#### 请求示例

```json
{
    "items": [
        {
            "question": "现行企业所得税法下，居民企业什么情况下取得的所得可能适用7.5%税率？",
            "reference_answer": "根据《企业所得税法》第二十八条，符合条件的小型微利企业，减按20%的税率征收企业所得税。国家需要重点扶持的高新技术企业，减按15%的税率征收企业所得税..."
        },
        {
            "question": "增值税进项税额抵扣需要满足哪些条件？",
            "reference_answer": "增值税进项税额抵扣需满足以下条件：1. 取得合法有效的增值税扣税凭证；2. 扣税凭证符合规定..."
        }
    ],
    "thread_count": 4
}
```

#### 响应参数

| 参数 | 类型 | 说明 |
|------|------|------|
| success | boolean | 请求是否成功 |
| count | int | 结果数量 |
| results | array | 结果列表 |
| results[].index | int | 序号（与请求顺序一致） |
| results[].question | string | 问题内容 |
| results[].answer | string | AI回答内容 |
| results[].reference_answer | string | 建议答案 |
| results[].success | boolean | 该条是否处理成功 |
| results[].scores | object/null | 评分结果，无建议答案时为 null |
| results[].scores.success | boolean | 评分是否成功 |
| results[].scores.accuracy_score | int | 答案准确性得分（0-60） |
| results[].scores.accuracy_reason | string | 答案准确性评分说明 |
| results[].scores.citation_score | int | 法条援引度得分（0-20） |
| results[].scores.citation_reason | string | 法条援引度评分说明 |
| results[].scores.summary_score | int | 总结完整度得分（0-20） |
| results[].scores.summary_reason | string | 总结完整度评分说明 |
| results[].scores.total_score | int | 总分（0-100） |

#### 响应示例 - 成功

```json
{
    "success": true,
    "count": 2,
    "results": [
        {
            "index": 0,
            "question": "现行企业所得税法下，居民企业什么情况下取得的所得可能适用7.5%税率？",
            "answer": "核心发现\n1. 根据《企业所得税法》第二十八条...",
            "reference_answer": "根据《企业所得税法》第二十八条...",
            "scores": {
                "success": true,
                "accuracy_score": 52,
                "accuracy_reason": "核心发现与参考答案基本一致，核心发现与详细内容无矛盾",
                "citation_score": 16,
                "citation_reason": "正确引用了企业所得税法第二十八条，但未引用实施条例相关条款",
                "summary_score": 17,
                "summary_reason": "核心发现涵盖了主要方面，个别特殊情况未提及",
                "total_score": 85
            },
            "success": true
        },
        {
            "index": 1,
            "question": "增值税进项税额抵扣需要满足哪些条件？",
            "answer": "核心发现\n1. 增值税进项税额抵扣需满足...",
            "reference_answer": "增值税进项税额抵扣需满足以下条件...",
            "scores": {
                "success": true,
                "accuracy_score": 45,
                "accuracy_reason": "部分内容与参考答案存在差异",
                "citation_score": 14,
                "citation_reason": "引用了部分法规但不够完整",
                "summary_score": 16,
                "summary_reason": "总结基本完整",
                "total_score": 75
            },
            "success": true
        }
    ]
}
```

#### 响应示例 - 请求参数错误

```json
{
    "success": false,
    "error": "请求体需包含 items 数组"
}
```

#### 响应示例 - 单条处理失败

```json
{
    "success": true,
    "count": 2,
    "results": [
        {
            "index": 0,
            "question": "正常问题",
            "answer": "AI回答内容...",
            "reference_answer": "参考答案...",
            "scores": { "success": true, "accuracy_score": 18, "...": "..." },
            "success": true
        },
        {
            "index": 1,
            "question": "导致异常的问题",
            "answer": "处理失败: Connection timeout",
            "reference_answer": "",
            "scores": null,
            "success": false
        }
    ]
}
```

#### 错误码

| HTTP状态码 | 说明 |
|------------|------|
| 200 | 请求成功（注意：单条处理失败不影响整体 HTTP 状态） |
| 400 | 请求参数错误（缺少 items、items 为空、某条缺少 question） |
| 500 | 服务器内部错误 |

---

### 2. 获取已保存的评分提示词

```
GET /saved_prompt
```

#### 响应示例

```json
{
    "prompt": "请作为一名专业的税务领域评估专家..."
}
```

---

### 3. 获取默认评分提示词

```
GET /default_prompt
```

#### 响应示例

```json
{
    "prompt": "请作为一名专业的税务领域评估专家..."
}
```

---

### 4. 保存评分提示词

```
POST /save_prompt
```

#### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| prompt | string | 是 | 提示词模板内容 |

#### 响应示例

```json
{
    "success": true,
    "message": "提示词已保存"
}
```

---

## 评分维度说明

评分采用 3 个维度，总分满分 100 分。

| 维度 | 字段 | 满分 | 说明 |
|------|------|------|------|
| 答案准确性 | accuracy_score | 60 | 核心发现与参考答案逐条语义对比；整体准确性；地区性关键点；核心发现与详细内容是否矛盾 |
| 法条援引度 | citation_score | 20 | 引用法条是否正确；引用列表与正文引用是否双向对应 |
| 总结完整度 | summary_score | 20 | 核心发现是否涵盖所有关键方面；隐含子问题是否回应 |

---

## 评分提示词模板变量

自定义 `scoring_prompt` 时，可使用以下占位变量：

| 变量 | 说明 |
|------|------|
| `{question}` | 用户提出的问题 |
| `{reference_answer}` | 建议答案（参考答案） |
| `{ai_answer}` | AI生成的回答 |

> 返回的 JSON 格式中需包含 `accuracy_score`、`citation_score`、`summary_score` 及对应的 `_reason` 字段。

---

## 调用示例

### cURL

```bash
curl -X POST http://ai.tech.tax.asia.pwcinternal.com:5001/api/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"question": "企业所得税税率是多少", "reference_answer": "标准税率为25%"}
    ]
  }'
```

### Python

```python
import requests

url = "http://ai.tech.tax.asia.pwcinternal.com:5001/api/evaluate"

payload = {
    "items": [
        {"question": "企业所得税税率是多少", "reference_answer": "标准税率为25%"},
        {"question": "增值税起征点是多少", "reference_answer": "按期纳税的为月销售额5000-20000元"}
    ],
    "thread_count": 4
}

response = requests.post(url, json=payload)
data = response.json()

for r in data["results"]:
    print(f"问题: {r['question']}")
    print(f"总分: {r['scores']['total_score'] if r['scores'] else 'N/A'}")
    print("---")
```

### Java

```java
import java.net.http.*;
import java.net.URI;

HttpClient client = HttpClient.newHttpClient();

String json = """
{
    "items": [
        {"question": "企业所得税税率是多少", "reference_answer": "标准税率为25%"}
    ]
}
""";

HttpRequest request = HttpRequest.newBuilder()
    .uri(URI.create("http://ai.tech.tax.asia.pwcinternal.com:5001/api/evaluate"))
    .header("Content-Type", "application/json")
    .POST(HttpRequest.BodyPublishers.ofString(json))
    .build();

HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
System.out.println(response.body());
```

---

## 注意事项

1. **超时时间**：每条问题处理包含多轮对话和评分，耗时约 30-120 秒，建议 HTTP 超时设置不低于 300 秒。
2. **并发控制**：`thread_count` 建议 2-4，过高可能触发服务端限流。
3. **评分依赖**：评分需要提供 `reference_answer`，否则 `scores` 返回 `null`。
4. **返回顺序**：`results` 数组中的 `index` 字段与请求 `items` 顺序一致，可直接按 `index` 对应。
5. **单条失败不影响整体**：某条处理失败时，整体接口仍返回 200，该条 `success` 为 `false`。
