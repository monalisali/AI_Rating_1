# 开发文档：四个Skill产出的多维评分与智能过滤模块

> **面向开发人员与Claude Code**  
> 本文档描述如何对四个Skill（条件-结论对、政策场景枚举、概念关系断言、时间约束）的原始输出进行多维评分、排序截断，生成高质量、低冗余的最终提示词。核心亮点：**不使用硬编码白名单、不依赖预定义词典（除停用词和少量正则模式）、在线计算逻辑独特度（局部IDF）、不设硬阈值，仅靠排序控制数量**。

---

## 1. 概述与背景

### 1.1 问题现状
四个Skill的原始输出往往包含大量与当前用户问题相关性不高、置信度低或结构冗余的内容。简单基于语义相似度（向量）设定阈值过滤，会导致核心信息（如“西部大开发15%税率可减半”）误删，而堆高阈值又会混入噪声。硬编码白名单无法适应无限变化的税务问题。

### 1.2 设计目标
- **自动适应任何用户问题**：无需预定义实体列表或白名单。
- **保留关键信息**：即使语义相似度不高，只要在逻辑独特度、实体重叠、结构重要性上得分高，也能被保留。
- **控制提示词长度**：通过排序后截断Top N，保证各产物类别数量可控。
- **完全在线**：逻辑独特度基于当前检索到的文章集计算局部IDF，无需离线全局语料。

### 1.3 整体流程
```
四个Skill原始输出
    │
    ├─► 对每个产物计算四维评分（语义、实体重叠、逻辑独特度、结构重要性）
    │
    ├─► 对每个类别分别按加权总分降序排序
    │
    ├─► 每个类别取 Top K（K 可配置，如条件-结论对取10条，概念断言取5条）
    │
    └─► 将保留的产物注入最终提示词（转换为自然语言引导）
```

---

## 2. 输入与输出

### 2.1 输入

**用户问题**：字符串  
**检索到的文章列表**：`List[Article]`，每个 `Article` 包含：
- `id`：ntpsid
- `title`：标题
- `content`：全文  
**四个Skill的原始输出**（JSON格式，与前文一致）：

- `condition_conclusion_pairs`：列表，元素含 `condition`, `conclusion`, `article_ids`, `text_for_similarity`（可拼接生成）
- `policy_scenes`：字符串列表
- `concept_relations`：列表，元素含 `entity_a`, `entity_b`, `relation_type`, `evidence`, `article_ids`
- `time_constraints`：列表，元素含 `policy_name`, `constraint_type`, `condition`, `article_ids`

### 2.2 输出

过滤后的产物字典，结构与输入一致，每个类别的数量控制在 `top_k` 以内，按评分从高到低排序。

---

## 3. 多维评分体系

### 3.1 维度与权重

| 维度 | 权重 | 说明 |
|------|------|------|
| 语义相关性 | 0.35 | 使用BGE-M3向量计算与用户问题的余弦相似度 |
| 实体重叠度 | 0.20 | 分词后计算Jaccard相似系数 |
| 逻辑独特度 | 0.25 | 基于当前文章集的局部IDF，衡量信息稀有性 |
| 结构重要性 | 0.20 | 检测数字、百分号、条款号、法规文号等结构化特征 |

### 3.2 评分范围与归一化

- 每个维度独立计算原始分数（可能范围不同），在**同一类别内部**进行线性归一化到 `[0, 1]`。
- 加权总分同样在类别内部归一化（可选，仅用于排序，非必须）。

---

## 4. 各维度计算详细实现

### 4.1 语义相关性

**方法**：使用已有的 BGE-M3 向量库。

**步骤**：
1. 对用户问题生成向量 `Q_vec`。
2. 对每个产物的 `text_for_similarity` 字段生成向量 `P_vec`（可离线预计算并缓存）。
3. 计算余弦相似度，结果即为原始分数。

**`text_for_similarity` 字段生成规则**：
- 条件-结论对：`condition + " → " + conclusion`
- 政策场景：场景标签本身
- 概念关系断言：`entity_a + " " + entity_b + " " + relation_type`
- 时间约束：`policy_name + " " + constraint_type + " " + condition`

### 4.2 实体重叠度

**方法**：中文分词 + Jaccard。

**实现**：
```python
import jieba

def tokenize(text):
    # 使用jieba精确模式
    words = jieba.lcut(text)
    # 过滤停用词、单字符、数字
    STOPWORDS = set(["的","了","是","在","和","与","或","以及","按照","根据","对","为","由","于","之","者","被","把","将","从","到","上","下","中","有","个","这","那","不","也","都"])
    filtered = []
    for w in words:
        if w in STOPWORDS:
            continue
        if len(w) == 1 and not w.isdigit():
            continue
        filtered.append(w)
    return set(filtered)

def jaccard_similarity(set1, set2):
    if not set1 and not set2:
        return 0.0
    inter = len(set1 & set2)
    union = len(set1 | set2)
    return inter / union

# 使用
q_tokens = tokenize(user_query)
p_tokens = tokenize(product_text)
overlap_score = jaccard_similarity(q_tokens, p_tokens)
```

### 4.3 逻辑独特度（局部IDF）

**核心**：基于本次检索到的文章集，计算每个词在文档集中的逆文档频率，然后求产物的平均IDF。

**步骤**：

#### 4.3.1 构建局部IDF字典

```python
import math
from collections import Counter

def build_local_idf(articles):
    N = len(articles)
    df = Counter()
    for art in articles:
        # 每篇文章内部去重
        words = set(tokenize(art['title'] + art['content']))  # 复用tokenize函数（去停用词）
        for w in words:
            df[w] += 1
    idf = {}
    for w, doc_cnt in df.items():
        idf[w] = math.log(N / (1 + doc_cnt))  # +1 平滑
    return idf
```

#### 4.3.2 计算单个产物的平均IDF

```python
def compute_avg_idf(product_text, idf_dict):
    words = tokenize(product_text)  # 返回set或list均可，但需保留重复词吗？一般采用词袋，可重复加权
    # 更精确：不去重，保留原词频，计算加权平均IDF
    # 此处我们使用list保留每个词
    words_list = [w for w in jieba.lcut(product_text) if w not in STOPWORDS and (len(w)>1 or w.isdigit())]
    if not words_list:
        return 0.0
    total = sum(idf_dict.get(w, 0.0) for w in words_list)
    return total / len(words_list)
```

#### 4.3.3 归一化

在同一类产物内部，所有 `avg_idf` 值线性归一化到 `[0,1]`.

### 4.4 结构重要性

**检测模式**：使用正则表达式检测产物文本中的结构化信息。

**加分项**（累计最高1.0）：
- 包含百分数（如 `15%`、`7.5%`）：+0.3
- 包含法规条款号（如 `第.*条`、`第八十六条`）：+0.3
- 包含年份（如 `2021年`、`2008年`）：+0.2
- 包含法规文号（如 `财税[2009]69号`、`国家税务总局公告[2012]12号`）：+0.2
- 包含数字加单位（如 `10万元`、`500万元`）：+0.1

**实现**：
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
    if re.search(r'\d{1,3}(?:万|亿)?元', text):
        score += 0.1
    return min(score, 1.0)
```

---

## 5. 评分归一化与排序截断

### 5.1 四维度原始分数计算

对于每个产物：
- `sim_raw`：语义相似度（已在0~1之间）
- `overlap_raw`：实体重叠度Jaccard（0~1）
- `uniqueness_raw`：平均IDF原始值（未经归一化）
- `struct_raw`：结构重要性（0~1）

### 5.2 对每个类别单独归一化

**目的**：消除不同产物类型之间原始分数范围差异。

```python
def normalize_scores(scores):
    min_s = min(scores)
    max_s = max(scores)
    if max_s == min_s:
        return [0.5] * len(scores)
    return [(s - min_s) / (max_s - min_s) for s in scores]
```

对每个类别的 `uniqueness_raw` 单独做归一化；其它维度（语义、重叠、结构）本身已在0~1之间，可不归一化（但若数据中有极端值，也可统一归一化）。

**建议**：所有四个维度都统一做归一化（在类别内部），避免量纲差异。

### 5.3 加权总分计算

```python
final_score = 0.35*sim_norm + 0.20*overlap_norm + 0.25*uniqueness_norm + 0.20*struct_norm
```

### 5.4 排序与截断

对每个类别：
```python
# 假设 products 是列表，每个元素包含 total_score
sorted_products = sorted(products, key=lambda x: x['total_score'], reverse=True)
kept = sorted_products[:top_k]
```

**推荐 `top_k` 值**（可根据实际token消耗调整）：
- 条件-结论对：10
- 政策场景：8
- 概念关系断言：5
- 时间约束：5

---

## 6. 最终提示词组装

过滤后的产物需要转换为自然语言，注入提示词。转换规则示例：

- **条件-结论对**：直接列出，格式 `- 条件：{condition} → 结论：{conclusion} （来源：{article_ids}）`
- **政策场景**：`- {scene}`
- **概念关系断言**：转换为短句，如 `- {entity_a} 与 {entity_b} 的关系为 {relation_type}`，若`relation_type`为`mutually_exclusive`则用“不能同时适用”。
- **时间约束**：转换为 `{policy_name}：{condition}`

---

## 7. 完整处理流程（主函数）

```python
def filter_skills_outputs(user_query, articles, skills_outputs, top_k_config):
    """
    skills_outputs: dict with keys: 'condition_conclusion_pairs', 'policy_scenes', 'concept_relations', 'time_constraints'
    top_k_config: dict with same keys, values are int
    """
    # 1. 构建局部IDF
    idf_dict = build_local_idf(articles)
    
    # 2. 为所有产物生成 text_for_similarity 和 计算各维度分数
    all_products = []  # 每个元素是 (category, product_dict)
    for cat, products in skills_outputs.items():
        for p in products:
            p['text_for_similarity'] = generate_text_for_similarity(cat, p)
            p['category'] = cat
            # 计算各维度原始分数
            p['sim_raw'] = cosine_similarity(query_vec, encode(p['text_for_similarity']))
            p['overlap_raw'] = jaccard_similarity(tokenize(user_query), tokenize(p['text_for_similarity']))
            p['uniqueness_raw'] = compute_avg_idf(p['text_for_similarity'], idf_dict)
            p['struct_raw'] = structural_score(p['text_for_similarity'])
            all_products.append((cat, p))
    
    # 3. 对每个类别分别归一化和排序截断
    filtered = {cat: [] for cat in skills_outputs.keys()}
    for cat in skills_outputs.keys():
        cat_products = [p for (c, p) in all_products if c == cat]
        if not cat_products:
            continue
        # 归一化 uniqueness_raw（其它维度也可归一化，但已在0-1范围）
        raw_uniqueness = [p['uniqueness_raw'] for p in cat_products]
        norm_uniqueness = normalize_scores(raw_uniqueness)
        for i, p in enumerate(cat_products):
            # 也可对 sim_raw 等做归一化（可选，若分布不均匀）
            p['total_score'] = (0.35 * p['sim_raw'] + 
                                0.20 * p['overlap_raw'] + 
                                0.25 * norm_uniqueness[i] + 
                                0.20 * p['struct_raw'])
        # 排序
        sorted_cat = sorted(cat_products, key=lambda x: x['total_score'], reverse=True)
        kept = sorted_cat[:top_k_config.get(cat, 5)]
        filtered[cat] = kept
    return filtered
```

---

## 8. 注意事项与性能优化

- **分词性能**：`jieba.lcut` 对每篇文章调用一次会产生开销。建议对文章内容、用户问题、产物文本分别缓存分词结果（例如使用 `functools.lru_cache`）。
- **向量编码**：产物文本的向量可离线预计算并存储；用户问题向量在线编码即可。
- **局部IDF计算**：每次查询都重新计算，文章集通常 < 50，词条数 < 5000，耗时可以忽略。
- **停用词表**：可维护一份通用中文停用词表（约200词），并追加税务领域的常见虚词（“规定”、“办法”、“通知”等）。
- **正则模式**：结构重要性中的模式可根据实际数据补充，例如匹配“减免”、“优惠”等关键词但需注意避免过度泛化。
- **截断数量**：建议根据实际模型最大token限制和测试效果调整。若token允许，可以适当增加。

---

## 9. 开发检查清单

- [ ] 实现 `tokenize` 函数（jieba + 停用词过滤）
- [ ] 实现 `build_local_idf` 函数
- [ ] 实现 `compute_avg_idf` 函数
- [ ] 实现 `jaccard_similarity` 函数
- [ ] 实现 `structural_score` 函数
- [ ] 实现 `generate_text_for_similarity` 映射
- [ ] 集成 BGE-M3 向量编码（复用现有）
- [ ] 实现主过滤流程 `filter_skills_outputs`
- [ ] 为每个类别配置合理的 `top_k` 值
- [ ] 测试典型问题（如7.5%税率），验证“西部大开发”相关产物被保留，且无关内容（如实质性运营）被截断
- [ ] 监控最终提示词长度在模型限制内（如 <8000 字符）

---

**文档版本**：1.0  
**最后更新**：2026-05-09