# 开发文档：四个Skill产物智能评分与过滤模块

> **面向开发人员与Claude Code**  
> 本文档描述如何对四个Skill（政策场景枚举、条件-结论对、概念关系断言、时间约束）的产物进行多维评分、排序截断，生成高质量、低冗余的最终提示词。**不依赖硬编码关键词白名单**，针对不同产物类型采用不同的权重配置和评分策略。概念断言单独使用“语义相似度 + 关系类型优先级”评分。

---

## 1. 概述

### 1.1 背景
四个Skill的输出原始产物数量较多，且包含大量与当前用户问题无关或低价值的内容。直接全部注入提示词会超出token限制并分散模型注意力。需要一种自动化的筛选方法，保留与问题最相关、逻辑最重要的产物，同时控制数量。

### 1.2 设计原则
- **无需预定义关键词列表**：所有评分基于语义向量、实体重叠、局部IDF（逻辑独特度）、结构特征，以及概念断言的关系类型优先级。
- **分类型配置**：政策场景、条件-结论对、概念关系断言、时间约束使用不同的评分公式。
- **截断控制**：每个类别按得分降序保留Top K条，保证提示词长度可控。

### 1.3 整体流程
```
原始产物列表
    │
    ├─► 对各类型产物分别计算多维评分（或语义+关系分）
    │
    ├─► 按得分降序排序
    │
    ├─► 每个类型保留Top K条
    │
    └─► 格式化后注入最终提示词
```

---

## 2. 输入数据格式

四个Skill的输出如下（示例）：

```json
{
  "policy_scenes": ["西部大开发优惠", "高新技术企业优惠", ...],
  "condition_conclusion_pairs": [
    {
      "condition": "企业既符合西部大开发15%优惠税率条件...",
      "conclusion": "可以按照企业适用税率（15%）计算的应纳税额减半征税",
      "article_ids": ["ART_9764", "ART_8003"],
      "text_for_scoring": "条件：... → 结论：..."
    }
  ],
  "concept_relations": [
    {
      "entity_a": "西部大开发优惠税率",
      "entity_b": "定期减免税减半期",
      "relation_type": "related_not_equal",
      "evidence": "...",
      "article_ids": ["ART_8003"],
      "text_for_scoring": "西部大开发优惠税率 定期减免税减半期 related_not_equal"
    }
  ],
  "time_constraints": [
    {
      "policy_name": "延续西部大开发企业所得税政策",
      "constraint_type": "valid_for",
      "condition": "自2021年1月1日至2030年12月31日",
      "article_ids": ["ART_46416"],
      "text_for_scoring": "延续西部大开发企业所得税政策 valid_for 自2021年1月1日至2030年12月31日"
    }
  ]
}
```

每个产物都应预先准备 `text_for_scoring` 字段，用于计算语义相似度等。

---

## 3. 通用辅助函数

### 3.1 语义相似度（使用BGE-M3）
```python
def encode(text):
    # 调用 BGE-M3 模型生成向量，可缓存结果
    pass

def cosine_similarity(vec1, vec2):
    return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))
```

### 3.2 实体重叠度（Jaccard）
```python
def tokenize(text):
    # jieba分词，去停用词，返回set
    pass

def jaccard_similarity(set1, set2):
    inter = len(set1 & set2)
    union = len(set1 | set2)
    return inter / union if union else 0.0
```

### 3.3 逻辑独特度（局部IDF）
基于当前检索到的所有文章（`articles`列表）计算IDF。
```python
def build_local_idf(articles):
    N = len(articles)
    df = Counter()
    for art in articles:
        words = set(tokenize(art['title'] + art['content']))
        for w in words:
            df[w] += 1
    idf = {w: math.log(N / (1 + cnt)) for w, cnt in df.items()}
    return idf

def compute_avg_idf(text, idf_dict):
    words = tokenize(text)  # 不去重，保留词频
    if not words:
        return 0.0
    total = sum(idf_dict.get(w, 0.0) for w in words)
    return total / len(words)
```

### 3.4 结构重要性
```python
def structural_score(text):
    score = 0.0
    if re.search(r'\d+(?:\.\d+)?%', text):
        score += 0.3
    if re.search(r'第[零一二三四五六七八九十百千万\d]+条', text):
        score += 0.3
    if re.search(r'\d{4}年', text):
        score += 0.2
    if re.search(r'(财税|国税|税务总局|公告)[\[\(]\d{4}[\d\]\)]', text):
        score += 0.2
    return min(score, 1.0)
```

---

## 4. 各产物类型评分与过滤

### 4.1 政策场景（Policy Scenes）

**特点**：短文本（通常2-6个汉字），稀有词语的IDF容易虚高，因此**不使用逻辑独特度**。

**评分公式**：
\[
\text{score} = 0.8 \times \text{semantic} + 0.1 \times \text{overlap} + 0.1 \times \text{structural}
\]

- `semantic`：与用户问题的余弦相似度
- `overlap`：Jaccard相似度
- `structural`：检测是否有数字、百分号等

**TopK配置**：`top_k = 10`

**代码示例**：
```python
def score_scenes(scenes, query_vec, query_tokens, idf_dict=None):
    scored = []
    for scene in scenes:
        vec = encode(scene)
        sem = cosine_similarity(query_vec, vec)
        overlap = jaccard_similarity(query_tokens, tokenize(scene))
        struct = structural_score(scene)
        total = 0.8 * sem + 0.1 * overlap + 0.1 * struct
        scored.append((scene, total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in scored[:top_k]]
```

---

### 4.2 概念关系断言（Concept Relations）

**特点**：通常为`entity_a + entity_b + relation_type + evidence`的短文本。**摒弃多维评分，采用“语义相似度 + 关系类型优先级加分”**。

**关系优先级加分（固定值）**：

| `relation_type` | 加分 |
|----------------|------|
| `related_not_equal` | +0.3 |
| `succession` | +0.3 |
| `mutually_exclusive` | +0.2 |
| `synonym`, `property_of` | +0.0 |

**评分公式**：
\[
\text{score} = \text{semantic} + \text{bonus}
\]

**TopK配置**：`top_k = 8`

**代码示例**：
```python
RELATION_BONUS = {
    "related_not_equal": 0.3,
    "succession": 0.3,
    "mutually_exclusive": 0.2,
    "synonym": 0.0,
    "property_of": 0.0
}

def score_concepts(concepts, query_vec):
    scored = []
    for concept in concepts:
        sem = cosine_similarity(query_vec, encode(concept['text_for_scoring']))
        bonus = RELATION_BONUS.get(concept['relation_type'], 0.0)
        total = sem + bonus
        scored.append((concept, total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:top_k_concept]]
```

---

### 4.3 条件-结论对（Condition-Conclusion Pairs）

**特点**：长文本（通常50-200字），信息丰富，适合多维评分。

**评分公式**：
\[
\text{score} = 0.5 \times \text{semantic} + 0.1 \times \text{overlap} + 0.2 \times \text{uniqueness} + 0.2 \times \text{structural}
\]

- `uniqueness`：局部IDF平均分（需提前基于检索文章计算）

**TopK配置**：`top_k = 12`

**代码示例**：
```python
def score_pairs(pairs, query_vec, query_tokens, idf_dict):
    scored = []
    for pair in pairs:
        text = pair['text_for_scoring']
        vec = encode(text)
        sem = cosine_similarity(query_vec, vec)
        overlap = jaccard_similarity(query_tokens, tokenize(text))
        unique = compute_avg_idf(text, idf_dict)
        struct = structural_score(text)
        total = 0.5 * sem + 0.1 * overlap + 0.2 * unique + 0.2 * struct
        scored.append((pair, total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in scored[:top_k_pairs]]
```

---

### 4.4 时间约束（Time Constraints）

**特点**：包含政策名称、生效/失效条件，结构信息（年份）很重要。

**评分公式**：
\[
\text{score} = 0.5 \times \text{semantic} + 0.2 \times \text{uniqueness} + 0.3 \times \text{structural}
\]

- 不使用实体重叠

**TopK配置**：`top_k = 6`

**代码示例**：
```python
def score_time_constraints(times, query_vec, idf_dict):
    scored = []
    for t in times:
        text = t['text_for_scoring']
        sem = cosine_similarity(query_vec, encode(text))
        unique = compute_avg_idf(text, idf_dict)
        struct = structural_score(text)
        total = 0.5 * sem + 0.2 * unique + 0.3 * struct
        scored.append((t, total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:top_k_time]]
```

---

## 5. 完整过滤流程（主函数）

```python
def filter_skills_outputs(user_query, articles, skills_outputs, top_k_config):
    """
    skills_outputs: dict with keys 'policy_scenes', 'condition_conclusion_pairs', 'concept_relations', 'time_constraints'
    top_k_config: dict with same keys, values are int
    """
    query_vec = encode(user_query)
    query_tokens = tokenize(user_query)
    idf_dict = build_local_idf(articles)   # 基于当前检索文章集

    # 政策场景
    filtered_scenes = score_scenes(
        skills_outputs['policy_scenes'],
        query_vec,
        query_tokens,
        idf_dict,
        top_k_config.get('policy_scenes', 10)
    )

    # 条件-结论对
    filtered_pairs = score_pairs(
        skills_outputs['condition_conclusion_pairs'],
        query_vec,
        query_tokens,
        idf_dict,
        top_k_config.get('condition_conclusion_pairs', 12)
    )

    # 概念关系断言
    filtered_concepts = score_concepts(
        skills_outputs['concept_relations'],
        query_vec,
        top_k_config.get('concept_relations', 8)
    )

    # 时间约束
    filtered_times = score_time_constraints(
        skills_outputs['time_constraints'],
        query_vec,
        idf_dict,
        top_k_config.get('time_constraints', 6)
    )

    return {
        'policy_scenes': filtered_scenes,
        'condition_conclusion_pairs': filtered_pairs,
        'concept_relations': filtered_concepts,
        'time_constraints': filtered_times
    }
```

---

## 6. 最终提示词组装

将过滤后的产物转换为自然语言，注入提示词。转换示例：

```python
def format_products(products):
    # 政策场景：直接列出
    scenes_block = "\n".join([f"- {s}" for s in products['policy_scenes']])
    # 条件-结论对：格式 "条件：... → 结论：..."
    pairs_block = "\n".join([f"- 条件：{p['condition']} → 结论：{p['conclusion']}" for p in products['condition_conclusion_pairs']])
    # 概念关系断言：转换为短句
    concepts_block = ""
    for c in products['concept_relations']:
        if c['relation_type'] == 'mutually_exclusive':
            concepts_block += f"- {c['entity_a']} 与 {c['entity_b']} 互斥，不能同时适用。\n"
        elif c['relation_type'] == 'related_not_equal':
            concepts_block += f"- {c['entity_a']} 与 {c['entity_b']} 相关但不可等同。\n"
        elif c['relation_type'] == 'succession':
            concepts_block += f"- {c['entity_b']} 替代 {c['entity_a']}，注意过渡期。\n"
        else:
            concepts_block += f"- {c['entity_a']} 是 {c['entity_b']} 的 {c['relation_type']}。\n"
    # 时间约束
    times_block = "\n".join([f"- {t['policy_name']}：{t['condition']}" for t in products['time_constraints']])
    return scenes_block, pairs_block, concepts_block, times_block
```

然后将这些块插入最终提示词的对应章节。

---

## 7. 配置参数汇总

| 产物类型 | 语义权重 | 重叠权重 | 独特度权重 | 结构权重 | 关系加分 | TopK |
|----------|----------|----------|------------|----------|----------|------|
| 政策场景 | 0.8 | 0.1 | 0.0 | 0.1 | — | 10 |
| 条件-结论对 | 0.5 | 0.1 | 0.2 | 0.2 | — | 12 |
| 概念断言 | 1.0 | — | — | — | 按类型 | 8 |
| 时间约束 | 0.5 | 0.0 | 0.2 | 0.3 | — | 6 |

---

## 8. 注意事项

- **语义相似度计算**：使用BGE-M3，建议对产物文本预编码并缓存，避免重复计算。
- **局部IDF**：每次查询都重新计算，因为检索文章集会变。若文章数很少（<10），IDF可能不可靠，可考虑降级为不使用独特度。
- **概念关系断言的`text_for_scoring`**：建议使用 `entity_a + " " + entity_b + " " + relation_type + " " + evidence`，证据可选，但尽量包含关键信息。
- **阈值预筛**：可选地对条件-结论对和概念断言先做语义预筛（如语义<0.3的直接丢弃），减少后续计算量。

---

## 9. 开发检查清单

- [ ] 实现BGE-M3向量编码与余弦相似度
- [ ] 实现jieba分词与停用词表
- [ ] 实现局部IDF计算函数
- [ ] 实现结构重要性检测
- [ ] 实现各类产物的评分函数
- [ ] 配置TopK参数
- [ ] 实现最终提示词组装
- [ ] 对典型测试问题进行端到端验证

---

**文档版本**：1.0  
**最后更新**：2026-05-10
