# AI回答稳定性模块 — 开发文档（B列场景）

> 本文档描述当Excel C列"文章内容"为空时，从读取B列"提示词"到拼接最终提示词的完整流程。

---

## 1. 整体流程

```
Excel B列(提示词) + A列(问题)
    │
    ├─ Step 1a: 第一次API调用 → 5007知识库API → 获取文章汇总列表
    │   message = A列问题, custom_system_prompt = B列提示词
    │
    ├─ Step 1b: 提取"文章汇总"章节
    │
    ├─ Step 1c: 填充文章获取模板 → 第二次API调用 → 获取文章原文
    │   message = "获取文章内容", custom_system_prompt = 填充后的模板
    │
    ├─ Step 1d: 按 NTPSID/文章N 标记拆分为独立文章
    │
    ├─ Step 2a: BGE-M3向量相似度分组
    │
    ├─ Step 2b: 每组调用合并Skill(并行) → LLM抽取4类信息
    │
    ├─ Step 2c: 跨组合并去重
    │
    ├─ Step 2d: 三层过滤（可选，当前禁用）
    │
    ├─ Step 2e: 多维评分过滤（分类型权重 → TopK截断）
    │
    ├─ Step 3:  C1-C7断言校验 + 转换为自然语言约束
    │
    └─ Step 4:  读取Prompt.md模板 → 填充4个Skill产物 → 最终提示词
```

---

## 2. 配置项一览

### 2.1 config.json（外部配置文件）

```json
{
    "llm_api_key": "sk-xxx",
    "llm_base_url": "https://genai-sharedservice-uat.cn.asia.pwcinternal.com",
    "model": "saas.glm-5.1",
    "group_max_chars": 12000,
    "group_sim_threshold": 0.6,
    "merge_cc_sim_threshold": 0.85,
    "thread_count": 4
}
```

| 配置项 | 说明 | 使用位置 |
|--------|------|----------|
| `llm_base_url` | LLM API地址 | `_call_llm` |
| `llm_api_key` | LLM API密钥 | `_call_llm` |
| `model` | LLM模型名 | `_call_llm`、`_get_articles_full` |
| `group_max_chars` | 文章分组每组最大字符数 | `_group_articles` |
| `group_sim_threshold` | 文章分组余弦相似度阈值 | `_group_articles` |
| `merge_cc_sim_threshold` | 条件-结论对合并相似度阈值 | `_merge_condition_pairs` |
| `thread_count` | Skill并行线程数 | `stability_process` |

### 2.2 answer_stability.py 内配置项

#### 三层过滤（当前禁用，`FILTER_ENABLED = False`）

```python
FILTER_ENABLED = False                    # 是否启用三层过滤
FILTER_REL_CC = 0.60                      # 条件-结论对相关性阈值
FILTER_REL_SCENE = 0.50                   # 政策场景相关性阈值
FILTER_REL_ASSERTION = 0.40               # 概念断言相关性阈值
FILTER_REL_TIME = 0.50                    # 时间约束相关性阈值
FILTER_CONF_CC = 0.60                     # 条件-结论对置信度阈值
FILTER_CONF_ASSERTION = 0.60              # 概念断言置信度阈值
FILTER_CONF_TIME = 0.80                   # 时间约束置信度阈值
SPECIFIC_KEYWORDS = ["西部大开发", "高新技术企业", ...]  # 专门性关键词
```

#### 多维评分过滤

```python
SCORE_FILTER_ENABLED = True               # 是否启用多维评分过滤

# 条件-结论对评分权重（信息丰富的长文本，适合多维评分）
SCORE_WEIGHT_CC_SIM = 0.7                 # 语义相关性
SCORE_WEIGHT_CC_OVERLAP = 0.1             # 实体重叠度
SCORE_WEIGHT_CC_UNIQUENESS = 0.1          # 逻辑独特度
SCORE_WEIGHT_CC_STRUCT = 0.1              # 结构重要性

# 政策场景评分权重（短文本，不使用逻辑独特度）
SCORE_WEIGHT_SCENE_SIM = 0.8              # 语义相关性
SCORE_WEIGHT_SCENE_OVERLAP = 0.1          # 实体重叠度
SCORE_WEIGHT_SCENE_STRUCT = 0.1           # 结构重要性

# 时间约束评分权重（不使用实体重叠）
SCORE_WEIGHT_TIME_SIM = 0.5               # 语义相关性
SCORE_WEIGHT_TIME_UNIQUENESS = 0.2        # 逻辑独特度
SCORE_WEIGHT_TIME_STRUCT = 0.3            # 结构重要性

# 概念断言关系类型优先级加分
RELATION_BONUS = {
    "related_not_equal": 0.3,
    "succession": 0.3,
    "mutually_exclusive": 0.2,
    "synonym": 0.0,
    "property_of": 0.0
}

# Top K 截断数量
TOP_K_CC = 15                             # 条件-结论对
TOP_K_SCENE = 10                          # 政策场景
TOP_K_ASSERTION = 10                      # 概念关系断言
TOP_K_TIME = 6                            # 时间约束

# 结构重要性评分
DOC_NUMBER_PREFIXES = ["财税", "国税", "税务总局", "公告", "国发"]

# 停用词表
STOPWORDS = set([
    "的", "了", "是", "在", "和", "与", "或", "以及", "按照", "根据",
    "对", "为", "由", "于", "之", "者", "被", "把", "将", "从", "到",
    "上", "下", "中", "有", "个", "这", "那", "不", "也", "都", "其",
    "等", "可", "应", "需", "要", "会", "能", "时", "如", "但", "并",
    "而", "及", "该", "此", "以", "当", "则", "若", "还", "已", "所",
    "优惠", "政策", "规定", "办法", "通知"
])
```

---

## 3. 各步骤详细说明与代码

### 3.1 Step 1a: 第一次API调用 — 获取文章汇总

调用 `_get_articles_full`，通过 `request_api` 发送到5007知识库API。

```python
def _extract_article_summary(text):
    """从API返回内容中提取"文章汇总"章节，到---或下一个##标题为止"""
    import re
    pattern = re.compile(r'##\s*文章汇总\s*\n(.*?)(?=\n---|\n##\s|\Z)', re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return ""


def _fill_article_template(article_list_text):
    """将文章列表文本填充到模板中，替换{{}}占位符"""
    template_path = os.path.join(ROOT_DIR, 'docs', '提高回答质量', '提示词',
                                  '通过文章URL或Title获取文章内容_模板填充.md')
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    filled = template.replace('{{}}', article_list_text)
    return filled


def _get_articles_full(question: str, max_rounds: int = 12, system_prompt: str = "") -> list:
    """多轮对话获取文章，返回每轮内容的列表（不丢弃中间轮次的文章原文）
    question: 搜索关键词（B列提示词或A列问题）
    system_prompt: 可选，通过custom_system_prompt传给API
    """
    session_id = ""
    current_message = question
    all_parts = []

    for round_i in range(max_rounds):
        try:
            api_response, session_id = request_api(current_message, session_id,
                                                     custom_system_prompt=system_prompt)
            content = parse_response(api_response)['full_content']
            if content and content.strip():
                all_parts.append(content)
                _log(f"[stability] 第{round_i + 1}轮获取完成（长度={len(content)}）")
            else:
                _log(f"[stability] 第{round_i + 1}轮返回为空")
            if is_confirmation_question(content):
                current_message = "同意，请继续，不需要调整。"
            elif is_incomplete_answer(content):
                current_message = "继续"
            else:
                break
        except Exception as e:
            _log(f"[stability] 第{round_i + 1}轮获取失败: {e}，继续下一轮")
            session_id = ""
            current_message = question

    _log(f"[stability] 文章获取完成，共{len(all_parts)}轮有内容，总长度={sum(len(p) for p in all_parts)}")
    return all_parts
```

**调用代码（在 `stability_process` 中）：**

```python
# 第一次API调用
raw_parts = _get_articles_full(question, system_prompt=kb_prompt or question)
raw_text = '\n\n'.join(raw_parts)
_log(f"[stability] row={row_num} Step1: API返回内容（长度={len(raw_text)}）:\n{raw_text}")

# 提取"文章汇总"章节
article_summary = _extract_article_summary(raw_text)
_log(f"[stability] row={row_num} Step1: 提取到文章汇总（长度={len(article_summary)}）:\n{article_summary}")
```

**API请求参数（`request_api`）：**

```python
url = 'https://ai.tech.tax.asia.pwcinternal.com:5007/api/chat-stream'
payload = {
    'message': question,              # A列问题
    'session_id': "",
    'model': "saas.glm-5.1",
    'custom_system_prompt': kb_prompt, # B列提示词
    'llm_api_key': "...",
    'llm_base_url': "..."
}
```

### 3.2 Step 1c: 第二次API调用 — 获取文章原文

将文章汇总填充到模板后，再次调用API获取文章原文。

```python
# 填充模板
filled_prompt = _fill_article_template(article_summary)
_log(f"[stability] row={row_num} Step2: 填充后的模板提示词（长度={len(filled_prompt)}）:\n{filled_prompt}")

# 第二次API调用
fetch_parts = _get_articles_full("获取文章内容", system_prompt=filled_prompt)
fetch_text = '\n\n'.join(fetch_parts)
_log(f"[stability] row={row_num} Step2: 第二次API返回内容（长度={len(fetch_text)}）:\n{fetch_text}")
```

### 3.3 Step 1d: 文章拆分

```python
def _parse_articles_from_text(text):
    """将文章内容按标记拆分为逐篇文章列表
    支持两种格式：
      ## {序号}. NTPSID: {数字id}
      ## 文章{序号}：{标题}
    以 --- 或不同的文章标题作为分隔
    """
    import re
    if not text or not text.strip():
        return []

    separator_pattern = re.compile(r'^---$', re.MULTILINE)
    segments = separator_pattern.split(text)

    ntpsid_pattern = re.compile(r'^##\s*\d+\.\s*NTPSID:\s*\d+', re.MULTILINE)
    article_title_pattern = re.compile(r'^##\s*文章\d+[：:]', re.MULTILINE)

    articles = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        matches = list(ntpsid_pattern.finditer(segment))
        if not matches:
            matches = list(article_title_pattern.finditer(segment))
        if not matches:
            articles.append(segment)
            continue
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
            article_text = segment[start:end].strip()
            if article_text:
                articles.append(article_text)

    if not articles:
        articles = [text.strip()]
    return articles
```

**调用：**

```python
article_parts = []
for part in fetch_parts:
    article_parts.extend(_parse_articles_from_text(part))
```

### 3.4 Step 2a: 文章分组（BGE-M3向量相似度）

```python
def _group_articles(article_parts, max_chars=None, sim_threshold=None):
    """按BGE-M3向量相似度对文章分组"""
    import numpy as np
    max_chars = max_chars or get_config('group_max_chars', 12000)
    sim_threshold = sim_threshold or get_config('group_sim_threshold', 0.6)

    if len(article_parts) <= 1:
        return [article_parts] if article_parts else []

    article_data = []
    for i, content in enumerate(article_parts):
        vec = _generate_embedding(content)
        article_data.append({'content': content, 'vec': vec, 'len': len(content)})

    groups = []
    for art in article_data:
        if art['len'] > max_chars:
            groups.append({'articles': [art], 'vecs': [art['vec']], 'total_len': art['len']})
            continue
        placed = False
        for group in groups:
            if group['total_len'] + art['len'] > max_chars:
                continue
            sims = [float(np.dot(art['vec'], v)) for v in group['vecs']]
            avg_sim = sum(sims) / len(sims)
            if avg_sim >= sim_threshold:
                group['articles'].append(art)
                group['vecs'].append(art['vec'])
                group['total_len'] += art['len']
                placed = True
                break
        if not placed:
            groups.append({'articles': [art], 'vecs': [art['vec']], 'total_len': art['len']})

    result = [[a['content'] for a in g['articles']] for g in groups]
    return result
```

### 3.5 Step 2b: 合并Skill调用（并行）

每组文章并行调用LLM，一次性抽取4类信息。

```python
COMBINED_SKILL_SYSTEM = """你是一个税务多维度信息抽取专家。请从给定的用户问题和文章中，同时抽取以下四类信息，一次性输出。

输出格式：一个JSON对象，包含以下四个字段：

1. "condition_conclusion_pairs"：条件-结论对数组，每个对象包含：
   - condition: 触发条件（一句话，客观可判断）
   - conclusion: 对应的结论/规则
   - article_ids: 来源文章ID列表（如["ART_44382"]）
   要求：每条规则独立，不合并多个条件；例外或限制条件单独拆分；不要添加原文没有的信息。

2. "policy_scenes"：政策场景标签数组，每个元素是一个简短标签（如"西部大开发优惠"）。
   要求：标签来源于文章中的政策名称或常见税务术语；具有区分度，避免过于宽泛；不要遗漏。

3. "concept_relations"：概念关系断言数组，每个对象包含：
   - entity_a: 概念A名称
   - entity_b: 概念B名称
   - relation_type: 关系类型，严格限定为以下六种之一：
     hypernym(A是B的下位), synonym(完全等价), related_not_equal(相关但不可等同),
     mutually_exclusive(互斥不能同时适用), succession(B替代A可能带过渡期), property_of(A是B的属性)
   - evidence: 原文证据（摘录1-2句）
   - article_ids: 来源文章ID列表
   要求：只抽取明确体现的关系，每条必须有原文证据；特别关注跨文章之间的概念关系。

4. "time_constraints"：时间约束数组，每个对象包含：
   - policy_name: 政策名称或简称
   - constraint_type: "valid_for"(适用) / "invalid_for"(不适用) / "transitional"(过渡期保留)
   - condition: 具体条件描述
   - article_ids: 来源文章ID列表
   要求：只抽取明确的时间限定；政策废止但保留存量时拆分为invalid_for和transitional两条。

如果某类信息不存在，输出空数组[]。
特别要求：重点关注跨文章之间的关系。
只输出JSON，不要其他解释。"""
```

**调用方式：**

```python
def _call_llm(system_prompt, user_prompt, temperature=0.01, max_tokens=2000, timeout=300, max_retries=3):
    """Call LLM with system prompt and temperature control"""
    base_url = get_config('llm_base_url', '').rstrip('/')
    key = get_config('llm_api_key', '')
    model = get_config('model', '')

    url = base_url + '/v1/chat/completions'
    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': system_prompt})
    messages.append({'role': 'user', 'content': user_prompt})

    body = {'model': model, 'messages': messages, 'temperature': temperature}
    if max_tokens > 0:
        body['max_tokens'] = max_tokens
    data = json.dumps(body).encode('utf-8)

    # ... HTTP请求 + 重试逻辑
```

每组文章的 user_prompt 构造：

```python
user_prompt = f"用户问题：{question}\n\n文章列表：\n{combined_text}"
resp = _call_llm(COMBINED_SKILL_SYSTEM, user_prompt, temperature=0, max_tokens=0, timeout=600)
parsed = _parse_json_response(resp)
```

### 3.6 Step 2c: 跨组合并去重

```python
products['condition_pairs'] = _merge_condition_pairs(all_cc_pairs)
products['scene_enum'] = _dedupe_strings(all_scenes)
products['assertions_raw'] = all_relations
products['time_constraints'] = _merge_time_constraints(all_time_constraints)
```

**合并条件-结论对**（语义相似度 >= `merge_cc_sim_threshold` 时合并 article_ids）：

```python
def _merge_condition_pairs(all_pairs, sim_threshold=None):
    """合并条件-结论对：语义相似度>=阈值则合并article_ids"""
    import numpy as np
    if not all_pairs:
        return []
    sim_threshold = sim_threshold or get_config('merge_cc_sim_threshold', 0.95)

    all_texts = [p.get('condition', '') + p.get('conclusion', '') for p in all_pairs]
    vec_cache = _batch_embeddings(all_texts)

    merged = []
    used_indices = set()
    for i, pair in enumerate(all_pairs):
        if i in used_indices:
            continue
        current = dict(pair)
        ids = list(current.get('article_ids', current.get('article_id', [])))
        if isinstance(ids, str):
            ids = [ids]
        text_i = all_texts[i]
        vec_i = vec_cache.get(text_i)
        if not vec_i:
            current['article_ids'] = ids
            if 'article_id' in current:
                del current['article_id']
            merged.append(current)
            continue
        for j in range(i + 1, len(all_pairs)):
            if j in used_indices:
                continue
            text_j = all_texts[j]
            vec_j = vec_cache.get(text_j)
            if vec_j:
                sim = float(np.dot(vec_i, vec_j))
                if sim >= sim_threshold:
                    other_ids = all_pairs[j].get('article_ids', all_pairs[j].get('article_id', []))
                    if isinstance(other_ids, str):
                        other_ids = [other_ids]
                    ids.extend([x for x in other_ids if x not in ids])
                    used_indices.add(j)
        current['article_ids'] = ids
        if 'article_id' in current:
            del current['article_id']
        merged.append(current)
    return merged
```

### 3.7 Step 2e: 多维评分过滤

```python
def _score_filter_products(user_query, article_texts, products):
    """多维评分过滤主函数：分类型评分→排序截断TopK
    - 条件-结论对：语义+重叠+独特度+结构
    - 概念断言：语义相似度+关系类型加分
    - 时间约束：语义+独特度+结构（无重叠）
    - 政策场景：语义+重叠+结构（无独特度）
    """
    import numpy as np

    idf_dict = _build_local_idf(article_texts)
    q_tokens = _tokenize(user_query)
    query_vec = _generate_embedding(user_query)

    def _score_multi_dim(items, text_fn, weights, top_k, label):
        """多维评分：仅计算权重非0的维度，仅对IDF归一化→加权总分→排序截断"""
        if not items:
            _log(f"[stability] {label}: 无数据，跳过")
            return []
        w_sim = weights.get('sim', 0)
        w_overlap = weights.get('overlap', 0)
        w_uniqueness = weights.get('uniqueness', 0)
        w_struct = weights.get('struct', 0)

        texts = [text_fn(item) for item in items]
        vec_cache = _batch_embeddings(texts)

        sim_scores, overlap_scores, uniqueness_scores, struct_scores = [], [], [], []
        for i, (item, text) in enumerate(zip(items, texts)):
            if w_sim > 0:
                vec = vec_cache.get(text)
                sim = float(np.dot(query_vec, vec)) if vec else 0.0
                sim_scores.append(sim)
            if w_overlap > 0:
                p_tokens = _tokenize(text)
                overlap = _jaccard_similarity(q_tokens, p_tokens)
                overlap_scores.append(overlap)
            if w_uniqueness > 0:
                avg_idf = _compute_avg_idf(text, idf_dict)
                uniqueness_scores.append(avg_idf)
            if w_struct > 0:
                struct = _structural_score(text)
                struct_scores.append(struct)

        uniqueness_norm = _normalize_scores(uniqueness_scores) if w_uniqueness > 0 else []
        for i, item in enumerate(items):
            total = 0.0
            if w_sim > 0:
                item['score_sim'] = round(sim_scores[i], 4)
                total += w_sim * sim_scores[i]
            if w_overlap > 0:
                item['score_overlap'] = round(overlap_scores[i], 4)
                total += w_overlap * overlap_scores[i]
            if w_uniqueness > 0:
                item['score_uniqueness'] = round(uniqueness_scores[i], 4)
                total += w_uniqueness * uniqueness_norm[i]
            if w_struct > 0:
                item['score_struct'] = round(struct_scores[i], 4)
                total += w_struct * struct_scores[i]
            item['total_score'] = round(total, 4)

        sorted_items = sorted(items, key=lambda x: x['total_score'], reverse=True)
        kept = sorted_items[:top_k]
        return kept

    result = {}

    # 1. 条件-结论对
    cc_items = [dict(p) for p in products.get('condition_pairs', [])]
    result['condition_pairs'] = _score_multi_dim(
        cc_items,
        lambda p: p.get('condition', '') + ' → ' + p.get('conclusion', ''),
        {'sim': SCORE_WEIGHT_CC_SIM, 'overlap': SCORE_WEIGHT_CC_OVERLAP,
         'uniqueness': SCORE_WEIGHT_CC_UNIQUENESS, 'struct': SCORE_WEIGHT_CC_STRUCT},
        TOP_K_CC, '条件-结论对')

    # 2. 概念断言（语义 + 关系加分）
    rel_items = [dict(r) for r in products.get('assertions_raw', [])]
    if not rel_items:
        result['concept_relations'] = []
    else:
        _assertion_text_fn = lambda r: (f"{r.get('entity_a', '')} {r.get('entity_b', '')}"
                                         + (f" {r.get('evidence', '')}" if r.get('evidence') else ''))
        rel_texts = [_assertion_text_fn(r) for r in rel_items]
        rel_vec_cache = _batch_embeddings(rel_texts)
        for item, text in zip(rel_items, rel_texts):
            vec = rel_vec_cache.get(text)
            sem = float(np.dot(query_vec, vec)) if vec else 0.0
            bonus = RELATION_BONUS.get(item.get('relation_type', ''), 0.0)
            item['score_sim'] = round(sem, 4)
            item['score_bonus'] = round(bonus, 4)
            item['total_score'] = round(sem + bonus, 4)
        sorted_rels = sorted(rel_items, key=lambda x: x['total_score'], reverse=True)
        result['concept_relations'] = sorted_rels[:TOP_K_ASSERTION]

    # 3. 时间约束
    tc_items = [dict(t) for t in products.get('time_constraints', [])]
    result['time_constraints'] = _score_multi_dim(
        tc_items,
        lambda t: f"{t.get('policy_name', '')} {t.get('constraint_type', '')} {t.get('condition', '')}",
        {'sim': SCORE_WEIGHT_TIME_SIM, 'uniqueness': SCORE_WEIGHT_TIME_UNIQUENESS,
         'struct': SCORE_WEIGHT_TIME_STRUCT},
        TOP_K_TIME, '时间约束')

    # 4. 政策场景
    scenes_raw = products.get('scene_enum', [])
    if not scenes_raw:
        result['policy_scenes'] = []
    else:
        scene_items = [{'label': s, '_original': s} for s in scenes_raw]
        scene_kept = _score_multi_dim(
            scene_items,
            lambda s: s['_original'],
            {'sim': SCORE_WEIGHT_SCENE_SIM, 'overlap': SCORE_WEIGHT_SCENE_OVERLAP,
             'struct': SCORE_WEIGHT_SCENE_STRUCT},
            TOP_K_SCENE, '政策场景')
        result['policy_scenes'] = [s['_original'] for s in scene_kept]

    return result
```

**辅助函数：**

```python
def _tokenize(text):
    """jieba分词+停用词过滤，返回set"""
    import jieba
    words = jieba.lcut(text)
    return set(w for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit()))

def _tokenize_list(text):
    """jieba分词+停用词过滤，返回list（保留词频）"""
    import jieba
    words = jieba.lcut(text)
    return [w for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]

def _jaccard_similarity(set1, set2):
    if not set1 and not set2:
        return 0.0
    inter = len(set1 & set2)
    union = len(set1 | set2)
    return inter / union if union > 0 else 0.0

def _build_local_idf(article_texts):
    import math
    from collections import Counter
    N = len(article_texts)
    if N == 0:
        return {}
    df = Counter()
    for text in article_texts:
        words = _tokenize(text)
        for w in words:
            df[w] += 1
    idf = {}
    for w, doc_cnt in df.items():
        val = math.log(N / (1 + doc_cnt))
        idf[w] = max(val, 0.0)
    return idf

def _compute_avg_idf(product_text, idf_dict):
    words_list = _tokenize_list(product_text)
    if not words_list:
        return 0.0
    total = sum(idf_dict.get(w, 0.0) for w in words_list)
    return total / len(words_list)

def _structural_score(text):
    import re
    score = 0.0
    if re.search(r'\d+(?:\.\d+)?%', text):
        score += 0.3
    if re.search(r'第[零一二三四五六七八九十百千万\d]+条', text):
        score += 0.3
    if re.search(r'\d{4}年', text):
        score += 0.2
    if re.search(r'(' + '|'.join(DOC_NUMBER_PREFIXES) + r')[\[\(]\d{4}[\d\]\)]', text):
        score += 0.2
    if re.search(r'[\[\(]\d{4}[\]\)]\s*\d+号', text):
        score += 0.2
    if re.search(r'\d{1,3}(?:万|亿)?元', text):
        score += 0.1
    return min(score, 1.0)

def _normalize_scores(scores):
    if not scores:
        return []
    min_s = min(scores)
    max_s = max(scores)
    if max_s == min_s:
        return [0.5] * len(scores)
    return [(s - min_s) / (max_s - min_s) for s in scores]
```

### 3.8 Step 3: 断言校验（C1-C7）

```python
def _validate_assertions(assertions):
    """Validate and clean concept relation assertions (rules C1-C7)"""
    if not assertions or not isinstance(assertions, list):
        return []
    cleaned = [dict(a) for a in assertions if isinstance(a, dict)]

    # C1: 删除自引用（entity_a 与 entity_b 相同）
    cleaned = [a for a in cleaned if a.get('entity_a', '') != a.get('entity_b', '')]

    # C2: 去重合并（相同三元组合并为一条，拼接 evidence）
    seen = {}
    for a in cleaned:
        key = (a.get('entity_a', ''), a.get('entity_b', ''), a.get('relation_type', ''))
        if key in seen:
            old_ev = seen[key].get('evidence', '')
            new_ev = a.get('evidence', '')
            if new_ev and new_ev not in old_ev:
                seen[key]['evidence'] = (old_ev + '；' + new_ev) if old_ev else new_ev
        else:
            seen[key] = dict(a)
    cleaned = list(seen.values())

    # C3: 同义+互斥冲突 → 删除互斥
    pair_rels = {}
    for a in cleaned:
        k = (a.get('entity_a', ''), a.get('entity_b', ''))
        pair_rels.setdefault(k, set()).add(a.get('relation_type', ''))
    conflict_c3 = {k for k, rels in pair_rels.items() if 'synonym' in rels and 'mutually_exclusive' in rels}
    if conflict_c3:
        cleaned = [a for a in cleaned if not (
            (a.get('entity_a', ''), a.get('entity_b', '')) in conflict_c3
            and a.get('relation_type', '') == 'mutually_exclusive')]

    # C4: 同义+下位冲突 → 删除下位
    conflict_c4 = {k for k, rels in pair_rels.items() if 'synonym' in rels and 'hypernym' in rels}
    if conflict_c4:
        cleaned = [a for a in cleaned if not (
            (a.get('entity_a', ''), a.get('entity_b', '')) in conflict_c4
            and a.get('relation_type', '') == 'hypernym')]

    # C5: 互斥对称补全（A互斥B → 自动补充 B互斥A）
    me_pairs = {(a.get('entity_a', ''), a.get('entity_b', ''))
                for a in cleaned if a.get('relation_type', '') == 'mutually_exclusive'}
    existing = {(a.get('entity_a', ''), a.get('entity_b', '')) for a in cleaned}
    for ea, eb in me_pairs:
        if (eb, ea) not in existing:
            cleaned.append({
                'entity_a': eb, 'entity_b': ea,
                'relation_type': 'mutually_exclusive',
                'evidence': 'derived: symmetric complement', 'derived': True
            })

    # C6: 已禁用
    # C7: 已禁用
    return cleaned
```

**转换为自然语言约束：**

```python
def _convert_constraints_to_text(cleaned_assertions):
    """Convert cleaned assertions to natural language constraint texts"""
    texts = []
    LQ = '“'
    RQ = '”'
    for a in cleaned_assertions:
        rt = a.get('relation_type', '')
        ea = a.get('entity_a', '')
        eb = a.get('entity_b', '')
        if rt == 'hypernym':
            texts.append(f'{LQ}{ea}{RQ}是{LQ}{eb}{RQ}的下位概念。禁止将二者视为并列或等同。')
        elif rt == 'synonym':
            texts.append(f'{LQ}{ea}{RQ}与{LQ}{eb}{RQ}为同义概念，回答时可直接等同替换。')
        elif rt == 'related_not_equal':
            texts.append(f'{LQ}{ea}{RQ}与{LQ}{eb}{RQ}相关但不可等同。禁止在推理中将二者相互替换或视为相同。')
        elif rt == 'mutually_exclusive':
            texts.append(f'{LQ}{ea}{RQ}与{LQ}{eb}{RQ}互斥，不能同时适用。')
        elif rt == 'succession':
            texts.append(f'{LQ}{eb}{RQ}替代{LQ}{ea}{RQ}，但注意是否存在过渡期保留（见时间约束）。')
        elif rt == 'property_of':
            texts.append(f'{LQ}{ea}{RQ}是{LQ}{eb}{RQ}的一个属性/参数，不等同于整体政策。')
    return texts
```

### 3.9 Step 4: 拼接最终提示词

```python
def _assemble_final_prompt(user_query, condition_pairs, scene_enum, constraint_texts, time_constraints):
    """以Prompt.md为模板，将4个Skill产物填入对应章节后返回完整提示词"""

    # 政策场景
    if isinstance(scene_enum, list) and scene_enum:
        scene_list = "\n".join(f"- {s}" if isinstance(s, str) else f"- {s}" for s in scene_enum)
    else:
        scene_list = "未提取到政策场景"

    # 概念约束
    constraints = "\n".join(f"- {t}" for t in constraint_texts) if constraint_texts else "无特殊概念约束"

    # 条件-结论对
    if isinstance(condition_pairs, list) and condition_pairs:
        cc_lines = []
        for cc in condition_pairs:
            cond = cc.get('condition', '')
            conc = cc.get('conclusion', '')
            cc_lines.append(f"- 条件：{cond} → 结论：{conc}")
        cc_text = "\n".join(cc_lines)
    else:
        cc_text = "未抽取到条件-结论对"

    # 时间约束
    if isinstance(time_constraints, list) and time_constraints:
        tc_lines = []
        for tc in time_constraints:
            ct = tc.get('constraint_type', '')
            cond = tc.get('condition', '')
            name = tc.get('policy_name', '')
            labels = {'valid_for': '适用', 'invalid_for': '不适用', 'transitional': '过渡期'}
            tc_lines.append(f"- {name}：{labels.get(ct, ct)} — {cond}")
        time_text = "\n".join(tc_lines)
    else:
        time_text = "无特殊时间约束"

    # 读取Prompt.md模板，按{{}}分割后填入4个Skill产物
    template_path = os.path.join(ROOT_DIR, 'Prompt.md')
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    parts = template.split('{{}}')
    skill_outputs = [scene_list, constraints, cc_text, time_text]

    filled = parts[0]
    for i, output in enumerate(skill_outputs):
        filled += output
        filled += parts[i + 1]

    return filled
```

---

## 4. 提示词汇总

### 4.1 文章获取模板（文件读取）

**文件路径：** `docs/提高回答质量/提示词/通过文章URL或Title获取文章内容_模板填充.md`

**作用：** Step 1c 中，将文章汇总列表填充到 `{{}}` 后作为第二次API调用的 `custom_system_prompt`，引导API通过URL或标题获取文章原文。

**使用方法：** `_fill_article_template()`

**模板内容：**

```markdown
你是一个智能税务政策法规搜索助手。
你的任务是根据用户的问题，检索并解读税收法规文件，生成严谨、有引用的回答。
请严格遵守下面的工作流程与输出要求，**每个步骤都是必须执行的强制流程**。
（不允许使用skills）
# 工作流程
## 1. 文章列表
{{}}

## 1. 根据url或者名称获取文章内容
- 如果有[URL]后的"[]"包含了合法的链接，就直接用链接获取文章内容。否则，就用[名称]后"[]"中的内容作为文章名称作为条件来搜索文章

# 可用工具
- `get_tax_policy_by_ntpsid`：按 NTPSID 获取法规全文与内部链接。


#不要使用 AskUserQuestion 工具**

# 输出要求
- 请列出所有检索到的文章原文，不要归纳总结
- 必须逐条调用 `get_tax_policy_by_ntpsid`，不得批量省略。
```

### 4.2 最终提示词模板（文件读取）

**文件路径：** `Prompt.md`（项目根目录）

**作用：** Step 4 中，作为最终提示词的模板。模板中有4个 `{{}}` 占位符，分别对应4个Skill产物。填充顺序：政策场景 → 核心法规指引 → 条件-结论对 → 时间约束。

**使用方法：** `_assemble_final_prompt()`

**模板中4个占位符位置：**

```markdown
## 5. 必须检查的政策场景
{{}}                        ← 填充政策场景列表

## 6. 核心法规指引与逻辑提示（非硬约束）
{{}}                        ← 填充概念关系约束文本

## 7. 可用的条件-结论对
{{}}                        ← 填充条件-结论对

## 8. 时间适用性约束
{{}}                        ← 填充时间约束
```

### 4.3 合并Skill系统提示词（硬编码）

**作用：** Step 2b 中，指导LLM从文章中一次性抽取4类信息（条件-结论对、政策场景、概念关系、时间约束）。

**使用方法：** `_call_llm(COMBINED_SKILL_SYSTEM, user_prompt, ...)`

```python
COMBINED_SKILL_SYSTEM = """你是一个税务多维度信息抽取专家。请从给定的用户问题和文章中，同时抽取以下四类信息，一次性输出。

输出格式：一个JSON对象，包含以下四个字段：

1. "condition_conclusion_pairs"：条件-结论对数组，每个对象包含：
   - condition: 触发条件（一句话，客观可判断）
   - conclusion: 对应的结论/规则
   - article_ids: 来源文章ID列表（如["ART_44382"]）
   要求：每条规则独立，不合并多个条件；例外或限制条件单独拆分；不要添加原文没有的信息。

2. "policy_scenes"：政策场景标签数组，每个元素是一个简短标签（如"西部大开发优惠"）。
   要求：标签来源于文章中的政策名称或常见税务术语；具有区分度，避免过于宽泛；不要遗漏。

3. "concept_relations"：概念关系断言数组，每个对象包含：
   - entity_a: 概念A名称
   - entity_b: 概念B名称
   - relation_type: 关系类型，严格限定为以下六种之一：
     hypernym(A是B的下位), synonym(完全等价), related_not_equal(相关但不可等同),
     mutually_exclusive(互斥不能同时适用), succession(B替代A可能带过渡期), property_of(A是B的属性)
   - evidence: 原文证据（摘录1-2句）
   - article_ids: 来源文章ID列表
   要求：只抽取明确体现的关系，每条必须有原文证据；特别关注跨文章之间的概念关系（替代、等价、互斥等）。

4. "time_constraints"：时间约束数组，每个对象包含：
   - policy_name: 政策名称或简称
   - constraint_type: "valid_for"(适用) / "invalid_for"(不适用) / "transitional"(过渡期保留)
   - condition: 具体条件描述
   - article_ids: 来源文章ID列表
   要求：只抽取明确的时间限定；政策废止但保留存量时拆分为invalid_for和transitional两条。

如果某类信息不存在，输出空数组[]。
特别要求：重点关注跨文章之间的关系——如果多篇文章涉及同一政策的不同阶段（如废止、替代、过渡期），务必在concept_relations和time_constraints中体现。
只输出JSON，不要其他解释。"""
```

### 4.4 最终回答系统提示词（硬编码）

**作用：** Step 5 中，作为生成最终回答的系统提示词（当前Step 5已禁用）。

**使用方法：** `_call_llm(FINAL_ANSWER_SYSTEM, final_prompt, ...)`

```python
FINAL_ANSWER_SYSTEM = "你是一个税务问答专家。请严格按以下结构和约束回答问题。"
```

### 4.5 多轮对话续传提示词（硬编码）

**作用：** `_get_articles_full` 中，多轮对话的后续轮次消息。

**使用方法：** `_get_articles_full()` 内部

```python
# 确认继续
current_message = "同意，请继续，不需要调整。"
# 不完整回答
current_message = "继续"
# 第二次API调用
message = "获取文章内容"
```
