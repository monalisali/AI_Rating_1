# 开发文档：优化四个Skill输出结果的后处理模块

> 本文档描述如何对四个Skill（条件-结论对、政策场景枚举、概念关系断言、时间约束）的输出进行自动化过滤、相关性筛选、置信度评估和冲突消解，生成高质量、低噪声的最终提示词，无需人工干预。

> **本文档中所有代码仅作说明、参考用，生成代码时还是以你为最高优先级**

---

## 1. 背景与目标

四个Skill产出的产物中，大量内容与用户问题无关或置信度低，直接拼接到提示词会导致：
- Token超限，模型注意力稀释
- 约束冲突，模型内部矛盾
- 冗余内容干扰核心推理

**目标**：通过纯代码规则（不调用LLM）对四个Skill的输出进行三层过滤：

1. **相关性过滤**：保留与用户问题语义相似度高的产物。
2. **置信度过滤**：只保留多源验证或高可靠性的断言。
3. **冲突消解**：当断言矛盾时，根据发布时间和专门性自动选择优先级高的。

**本文档不包含**：
- 实体名称精确匹配（已移除）
- 法规层级置信度（已移除）
- 与用户问题的动态相关性增强（已移除）

---

## 2. 输入数据结构

四个Skill的输出为以下JSON对象（示例）：

```json
{
  "condition_conclusion_pairs": [
    {
      "condition": "居民企业执行西部大开发优惠政策，且处于定期减免税的减半期内",
      "conclusion": "可以按照企业适用税率计算的应纳税额减半征税",
      "article_ids": ["ART_9764", "ART_8003"],
      "source_count": 2,
      "publish_date": "2009-12-31"
    }
  ],
  "policy_scenes": [
    "西部大开发优惠",
    "高新技术企业优惠",
    "农林牧渔业优惠",
    "公共基础设施项目优惠"
  ],
  "concept_relations": [
    {
      "entity_a": "西部大开发15%优惠税率",
      "entity_b": "定期减免税减半期",
      "relation_type": "related_not_equal",
      "source_count": 2,
      "publish_date": "2012-05-25",
      "evidence": "...",
      "confidence": 0.9
    }
  ],
  "time_constraints": [
    {
      "policy_name": "西部大开发15%税率",
      "constraint_type": "valid_for",
      "condition": "2021年1月1日至2030年12月31日",
      "article_ids": ["ART_46416"],
      "publish_date": "2020-04-23"
    }
  ]
}
```

**字段说明**：
- `source_count`：该断言出现的文章数量（多源验证指标）
- `publish_date`：法规发布日期（用于冲突消解时的“时间优先”原则）
- `confidence`：置信度（0~1），可根据`source_count`和证据质量计算（详见第4节）

---

## 3. 相关性过滤

### 3.1 过滤方法

使用BGE-M3向量模型（已在分组方案中使用）计算用户问题与每个产物的语义相似度。

**步骤**：
1. 对用户问题生成向量 `Q_vec`。
2. 对每个产物的文本字段生成向量（离线预计算或在过滤时实时计算，推荐预计算）。
3. 计算余弦相似度，保留超过阈值的产物。

### 3.2 各产物的文本字段与阈值

| 产物类型 | 用于相似度计算的文本字段 | 推荐阈值 |
|----------|--------------------------|----------|
| 条件-结论对 | `condition + " → " + conclusion` | 0.60 |
| 政策场景标签 | 标签文本本身 | 0.50 |
| 概念关系断言 | `entity_a + entity_b + relation_type` | 0.40 |
| 时间约束 | `policy_name + constraint_type + condition` | 0.50 |

### 3.3 特殊情况处理

- 若某个产物类型经过滤后为空，则**不强制保留**，允许该类型缺失（后续回答可依赖其他类型产物）。
- 记录被过滤掉的产物数量到日志，便于调试。

### 3.4 代码示例

```python
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

def filter_by_relevance(products, query_vector, product_vectors, threshold):
    """
    products: list of dict, 每个元素包含 'text_for_similarity' 字段
    product_vectors: list of vectors, 与 products 一一对应
    """
    relevant = []
    for prod, vec in zip(products, product_vectors):
        sim = cosine_similarity([query_vector], [vec])[0][0]
        if sim >= threshold:
            prod['relevance_score'] = sim
            relevant.append(prod)
    return relevant

# 调用示例
query_vec = bge_m3.encode(user_query)
relevant_pairs = filter_by_relevance(
    condition_pairs, 
    query_vec, 
    [p['vector'] for p in condition_pairs], 
    threshold=0.6
)
```

**注意**：`product_vectors` 需提前生成并存储，或在使用时实时调用BGE-M3生成（性能较低）。

---

## 4. 置信度过滤

### 4.1 置信度计算规则

不依赖法规层级，仅使用以下两个指标：

1. **多源验证**：
   - `source_count >= 2` → `confidence = 0.9`
   - `source_count == 1` → `confidence = 0.6`（基础值）

2. **证据质量**（可选）：
   - 若`evidence`字段长度 > 100字符且包含具体条款号 → `confidence += 0.1`（上限1.0）

**最终置信度** = 基础值 + 质量加分，截断在[0,1]范围内。

### 4.2 过滤阈值

| 产物类型 | 最低置信度 |
|----------|------------|
| 条件-结论对 | 0.60 |
| 概念关系断言 | 0.70 |
| 时间约束 | 0.80（时间信息通常可靠，提高阈值） |
| 政策场景标签 | 不设置信度过滤，仅依赖相关性（标签本身无置信度概念） |

### 4.3 代码示例

```python
def compute_confidence(assertion):
    """计算断言置信度"""
    conf = 0.6  # 单源基础值
    if assertion.get('source_count', 1) >= 2:
        conf = 0.9
    # 证据质量加分
    evidence = assertion.get('evidence', '')
    if len(evidence) > 100 and ('第' in evidence or '条' in evidence):
        conf = min(1.0, conf + 0.1)
    return conf

def filter_by_confidence(products, min_conf):
    return [p for p in products if compute_confidence(p) >= min_conf]
```

---

## 5. 冲突消解

### 5.1 冲突类型检测

| 冲突类型 | 检测条件 | 示例 |
|----------|----------|------|
| 互斥断言冲突 | 同一对概念同时出现 `mutually_exclusive` 和 `related_not_equal` 或 `synonym` | |
| 结论冲突 | 相同`condition`对应不同`conclusion` | |
| 关系冲突 | `A hypernym B` 和 `B hypernym A` 同时存在 | |

**分组策略**：
- 对于概念关系断言：按 `(entity_a, entity_b)` 规范化排序后的元组分组。
- 对于条件-结论对：按 `condition` 原文分组。

### 5.2 消解规则（无法规层级）

仅使用以下两条规则，按优先级顺序：

| 优先级 | 规则 | 说明 |
|--------|------|------|
| 1 | **发布时间优先** | 比较`publish_date`字段，选择日期较晚的断言（假定新法优于旧法）。 |
| 2 | **专门性优先** | 若一条断言的`evidence`或`entity`中明确包含限定词（如“西部大开发企业”“高新技术企业”），且另一条是通用规则，则专门性断言优先。判断方法：检查文本中是否包含具体主体名称（正则匹配）。 |

**注意**：若两条断言发布时间相同且专门性相同，则保留`source_count`较大者；若仍相同，保留任意一条并记录日志。

### 5.3 专门性判断实现

```python
def is_specific(assertion):
    """判断断言是否具有专门性（针对特定主体）"""
    text = json.dumps(assertion)  # 合并所有文本字段
    specific_keywords = [
        "西部大开发", "高新技术企业", "小型微利", "软件企业", 
        "集成电路", "经济特区", "浦东新区", "海南自贸港"
    ]
    for kw in specific_keywords:
        if kw in text:
            return True
    return False
```

### 5.4 冲突消解代码示例

```python
def resolve_conflicts(assertions):
    # 按分组键分组
    groups = {}
    for a in assertions:
        key = get_group_key(a)  # 例如 (entity_a, entity_b) 或 condition
        groups.setdefault(key, []).append(a)
    
    resolved = []
    for key, group in groups:
        if len(group) == 1:
            resolved.append(group[0])
        else:
            # 排序：发布时间晚 > 专门性高 > source_count大
            group.sort(key=lambda x: (
                x.get('publish_date', '1900-01-01'),
                is_specific(x),
                x.get('source_count', 0)
            ), reverse=True)
            resolved.append(group[0])
            # 记录丢弃的断言
            for discarded in group[1:]:
                log_conflict(key, discarded)
    return resolved
```

---

## 6. 完整过滤流程（主函数）

```python
def filter_skills_outputs(user_query, skills_outputs):
    """
    输入：
        user_query: 字符串
        skills_outputs: dict，包含四个Skill的输出
    输出：
        filtered_products: dict，包含过滤后的产物（可直接用于组装最终提示词）
    """
    query_vec = bge_m3.encode(user_query)
    
    # 1. 条件-结论对
    pairs = skills_outputs['condition_conclusion_pairs']
    for p in pairs:
        p['text_for_similarity'] = p['condition'] + ' → ' + p['conclusion']
    pairs = filter_by_relevance(pairs, query_vec, threshold=0.6)
    pairs = filter_by_confidence(pairs, min_conf=0.6)
    
    # 2. 政策场景标签（仅相关性过滤）
    scenes = skills_outputs['policy_scenes']
    scenes = filter_by_relevance(scenes, query_vec, threshold=0.5)
    
    # 3. 概念关系断言
    rels = skills_outputs['concept_relations']
    for r in rels:
        r['text_for_similarity'] = f"{r['entity_a']} {r['entity_b']} {r['relation_type']}"
    rels = filter_by_relevance(rels, query_vec, threshold=0.4)
    rels = filter_by_confidence(rels, min_conf=0.7)
    rels = resolve_conflicts(rels)
    
    # 4. 时间约束
    times = skills_outputs['time_constraints']
    for t in times:
        t['text_for_similarity'] = f"{t['policy_name']} {t['constraint_type']} {t['condition']}"
    times = filter_by_relevance(times, query_vec, threshold=0.5)
    times = filter_by_confidence(times, min_conf=0.8)
    
    return {
        'condition_conclusion_pairs': pairs,
        'policy_scenes': scenes,
        'concept_relations': rels,
        'time_constraints': times
    }
```

---

## 7. 最终提示词组装

过滤后的产物转化为自然语言。**不要求**将概念关系断言转为硬约束语句，而是直接引用法规原文和引导性提示。

### 7.1 概念关系断言 -> 引导性提示

```python
def format_concept_relations(rels):
    lines = []
    for r in rels:
        # 只保留高置信度且无冲突的，不生成绝对化语句
        lines.append(f"- {r['entity_a']} 与 {r['entity_b']}：{r['relation_type']}（依据{r.get('evidence', '')[:100]}）")
    return "\n".join(lines)
```

### 7.2 最终提示词示例片段

```text
## 必须检查的政策场景
{每行一个场景}

## 相关概念关系提示（供参考）
{format_concept_relations}

## 时间约束
{每行一个时间约束}

## 可用的条件-结论对
{JSON格式或列表}
```

---

## 8. 异常处理与降级

- **相关性过滤后某产物为空**：跳过该产物的注入，不影响其他产物。
- **冲突消解后无断言保留**：记录警告日志，后续不注入概念关系部分。
- **所有产物均为空**：回退到不使用任何Skill产物的基础提示词（仅用户问题+检索文章）。

---

## 9. 检查清单（Claude Code 实现验证）

- [ ] 实现BGE-M3向量编码函数。
- [ ] 实现相关性过滤函数（阈值可配置）。
- [ ] 实现置信度计算与过滤函数。
- [ ] 实现冲突消解函数（发布时间+专门性）。
- [ ] 集成到主流程，替换原有直接拼接逻辑。
- [ ] 对典型测试问题（如7.5%税率）进行端到端测试，验证过滤后产物数量合理且无矛盾。

---

## 10. 其它要求
1. 所有的阈值都要做成可配置。配置项写在answer_stability.py中即可，而且每个配置项都要加上注释，说明配置项的用处
2. 关键步骤加上log
3. 回答稳定性结果文件（包括自动生成和下载）中增加栏位：
- 每个skill都增加一个栏位，记录过滤后的数据。如：条件-结论skill已有“条件-结论对(处理前)”和条件-结论对(处理后)，就再增加“条件-结论对（过滤后）”，其它skill类似。
---