# 税务AI系统开发文档：方案A - 全Skill分组处理与跨文章关系合并

> 描述如何通过分组处理（将多篇相关文章同时输入Skill）解决四个Skill中的跨文章关系丢失问题。所有Skill（条件-结论对、政策场景枚举、概念关系断言、时间约束）均采用分组处理。单篇超长文章（>12,000字符）独立成组。相似度计算仅使用BGE-M3向量模型。所有提示词可直接使用。

> **本文档中所有代码仅作说明、参考用，生成代码时还是以你为最高优先级**
---

## 1. 问题定义
目前answer_stability.py中处理4个skill的方式为：
- Phase 1：逐篇并行抽取。每篇文章分别调用4个Skill，所有任务并行执行。比如有3篇文章，就产生 3×4=12 个并行任务
- Phase 2：跨文章概念关系发现（仅 Skill 3，且文章数>1时才执行）
  - 把 Phase 1 中所有文章的概念关系断言汇总，让模型发现跨文章之间的关系（如不同文章中的同义概念、上下位关系等），新增断言追加到结果中
- 最后再合并去重

这会导致导致：
- **跨文章概念关系**（如“财税〔2001〕202号被财税〔2011〕58号替代且存量保留”）无法被抽取。
- **跨文章时间线**（同一政策的生效、废止、过渡期分散在多篇公告中）无法被完整拼合。
- **跨文章条件组合**（如文章A的条件 + 文章B的结论）被忽略。
- **场景枚举**可能因单篇文章视角不全而遗漏标签。

**解决目标**：将所有检索到的文章分成若干语义相关的组，每组内文章总token控制在模型上下文窗口内（12,000字符），每组调用所有四个Skill（合并为一次LLM调用或分别调用），从而抽取跨文章关系。

---

## 2. 整体流程（全Skill分组处理版）

```
检索文章列表
    │
    ├─► 预处理器：超长文章（>12,000字符）标记为独立分组
    │
    ├─► 分组器（基于BGE-M3向量相似度 + token限制） → 输出多个文章分组
    │
    ├─► 对每个分组：
    │     ├─► 调用“全Skill组合调用”（一次LLM同时输出四个产物）
    │     └─► 输出该分组的所有产物
    │
    ├─► 收集所有分组的产物 → 规则校验层（去重、冲突解决、传递合并）
    │
    └─► 输出最终四个产物（供后续动态提示词组装）
```

**输入**：
- `user_query`: 字符串
- `articles`: 列表，每个元素包含 `id`, `title`, `content`, `vector`（BGE-M3向量，必须）

**输出**：
- `condition_conclusion_pairs`: 列表（全局合并后）
- `policy_scenes`: 列表（全局并集）
- `concept_relations`: 列表（全局清洗后）
- `time_constraints`: 列表（全局清洗后）

---

## 3. 分组器实现（基于BGE-M3向量）

### 3.1 分组目标
- 每组总字符数 ≤ 12,000（约3-4k tokens，为128k模型留足余量）。
- 同一组内的文章语义高度相关（基于BGE-M3向量余弦相似度 ≥ 0.6）。
- 单篇超过12,000字符的文章独立成组。

### 3.2 向量要求
- 所有文章必须预先通过 **BGE-M3** 模型生成向量（768维或1024维，推荐使用 `BAAI/bge-m3`）。
- 相似度计算仅使用余弦相似度，不再使用关键词重叠。
- 可以参考secen_vector.py中BEG-M3的实现

### 3.3 分组算法（Python伪代码）

```python
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

def group_articles(articles, max_chars=12000, sim_threshold=0.6):
    """
    articles: list of dict with keys: id, title, content, vector (BGE-M3)
    max_chars: 每组最大字符数
    sim_threshold: 余弦相似度阈值，低于此值的不与其同组
    """
    def similarity(vec1, vec2):
        return cosine_similarity([vec1], [vec2])[0][0]
    
    # 按原始检索分数降序排序（可选，若无则按ID）
    sorted_articles = sorted(articles, key=lambda x: x.get('score', 0), reverse=True)
    groups = []
    for art in sorted_articles:
        art_len = len(art['title'] + art['content'])
        # 单篇超长：独立成组
        if art_len > max_chars:
            groups.append([art])
            continue
        
        placed = False
        for group in groups:
            # 字符数检查
            total_chars = sum(a['char_len'] for a in group) + art_len
            if total_chars > max_chars:
                continue
            # 相似度检查：与组内任一文章平均相似度≥阈值
            group_vecs = [g['vector'] for g in group]
            avg_sim = np.mean([similarity(art['vector'], v) for v in group_vecs])
            if avg_sim >= sim_threshold:
                group.append(art)
                placed = True
                break
        if not placed:
            groups.append([art])
    
    # 为每个组计算总字符数（用于后续日志）
    for g in groups:
        g['total_chars'] = sum(len(a['title']+a['content']) for a in g)
    return groups
```

---

## 4. 全Skill组合调用（分组内处理）

为减少LLM调用次数，将四个Skill合并为一次LLM调用。模型需要同时输出以下四个JSON数组。

### 4.1 示例（输入两篇文章，输出四个产物）

**输入**（两篇文章，内容略，同之前西部大开发案例）  
**模型输出**：
```json
{
  "condition_conclusion_pairs": [
    {
      "condition": "西部地区新办交通、电力、水利、邮政、广播电视企业，且业务收入占70%以上",
      "conclusion": "内资企业：第1-2年免征所得税，第3-5年减半征收",
      "article_ids": ["ART_001"]
    },
    {
      "condition": "2010年12月31日前新办的西部鼓励类企业",
      "conclusion": "可继续享受两免三减半至期满",
      "article_ids": ["ART_002"]
    }
  ],
  "policy_scenes": ["西部大开发优惠", "两免三减半", "过渡期优惠"],
  "concept_relations": [
    {
      "entity_a": "财税〔2001〕202号",
      "entity_b": "财税〔2011〕58号",
      "relation_type": "succession",
      "evidence": "财税〔2011〕58号第五条规定财税〔2001〕202号停止执行，但第三条规定存量企业过渡期保留。",
      "article_ids": ["ART_001", "ART_002"]
    }
  ],
  "time_constraints": [
    {
      "policy_name": "财税〔2001〕202号两免三减半",
      "constraint_type": "invalid_for",
      "condition": "2011年1月1日以后新办的企业",
      "article_ids": ["ART_002"]
    },
    {
      "policy_name": "财税〔2001〕202号两免三减半",
      "constraint_type": "transitional",
      "condition": "2010年12月31日前已新办的企业，可继续享受至期满",
      "article_ids": ["ART_002"]
    }
  ]
}
```

---

## 5. 单篇超长文章处理

如果某篇文章的总字符数（标题+正文）超过12,000，则**独立成为一个分组**（即组内只有该文章）。这样做的前提是模型上下文窗口足够大（现代主流模型通常支持≥32k tokens，可容纳约12万字符）。如果模型上下文窗口小于该文章长度，则需要对文章进行切片（本方案不强制，建议升级模型）。

**实现**：
```python
def preprocess_articles(articles, max_chars=12000):
    """
    将超长文章标记为强制独立分组，无需切片。
    """
    for art in articles:
        art['len'] = len(art['title'] + art['content'])
        art['is_long'] = art['len'] > max_chars
    return articles
```

在分组器中，遇到`is_long == True`的文章，直接创建仅包含该文章的新组。

---

## 6. 跨组合并规则（适用于所有产物）

收集所有分组的输出后，需要进行合并、去重、冲突解决。

### 6.1 条件-结论对合并
- 去重：如果两个pair的`condition`和`conclusion`语义相似度≥0.95（可用BGE-M3计算），合并`article_ids`列表。
  - 条件-结论合并的语义相似值要可配置的
- 无额外冲突处理。

### 6.2 政策场景合并
- 直接取所有分组输出的`policy_scenes`的**并集**，去重（字符串完全匹配）。

### 6.3 概念关系断言合并
复用requirements_回答稳定性.md中的规则校验层（C1-C7, M2, M3等）。具体规则见附录。

### 6.4 时间约束合并
- 同一政策的同一`constraint_type`，若条件相同则合并`article_ids`。
- 若条件不同（如一个说“2010年前”，一个说“2011年前”），保留两者（不合并），由最终提示词中的模型自行判断逻辑关系。

---

## 7. 最终输出格式

```python
final_products = {
    "condition_conclusion_pairs": [ /* 全局列表 */ ],
    "policy_scenes": [ /* 全局唯一场景列表 */ ],
    "concept_relations": [ /* 全局清洗后断言列表 */ ],
    "time_constraints": [ /* 全局时间约束列表 */ ]
}
```

---


```python
def main_pipeline(user_query, articles):
    # 0. 确保每篇文章有BGE-M3向量（若没有，需提前调用BGE-M3生成）
    for art in articles:
        if 'vector' not in art:
            art['vector'] = bge_m3.encode(art['title'] + art['content'])
    
    # 1. 分组
    groups = group_articles(articles)
    
    # 2. 处理每个分组（可以是并行）
    all_condition_pairs = []
    all_scenes = []
    all_concept_rels = []
    all_time_constraints = []
    
    for group in groups:
        result = call_combined_skill(user_query, group)   # LLM调用，返回JSON
        all_condition_pairs.extend(result['condition_conclusion_pairs'])
        all_scenes.extend(result['policy_scenes'])
        all_concept_rels.extend(result['concept_relations'])
        all_time_constraints.extend(result['time_constraints'])
    
    # 3. 合并去重
    final_condition_pairs = merge_condition_pairs(all_condition_pairs)
    final_scenes = list(set(all_scenes))
    final_concept_rels = apply_all_rules(all_concept_rels)   # 包含M2, M3等
    final_time_constraints = merge_time_constraints(all_time_constraints)
    
    return {
        "condition_conclusion_pairs": final_condition_pairs,
        "policy_scenes": final_scenes,
        "concept_relations": final_concept_rels,
        "time_constraints": final_time_constraints
    }
```

---

## 8. 注意事项
- **向量计算**：必须使用BGE-M3模型。
- **相似度阈值**：分组时的 `sim_threshold=0.6` 可根据实际情况调整（建议范围0.5~0.7）。
   - 数值要改成可配置的
- **温度参数**：组合Skill调用使用 `temperature=0` 确保确定性。
- **超长文章**：若单篇文章超过12,000但小于模型上下文上限（如50k），独立分组即可；若超过模型上限，需更换模型或切片（后者不在本方案范围内）。
   - 上限12,000要改成可配置的

---
