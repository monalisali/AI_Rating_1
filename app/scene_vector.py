"""
场景强化向量检索完整实现代码（税务文章专用）
Embedding 支持：BGE-M3（本地） / doubao-embedding-vision（火山方舟API）
功能：自动生成场景强化摘要+双向量检索+结果对比
使用说明：1. CONFIG中切换 embedding_provider  2. 放入Excel  3. 运行即可
"""
import pandas as pd
import numpy as np
import requests
import json
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Dict

# --------------------------
# 配置参数（需替换为你的实际配置）
# --------------------------
CONFIG = {
    "llm_provider": "doubao",
    # Embedding 方式切换：bge_m3 = 本地 BGE-M3 模型，doubao = 火山方舟 doubao-embedding-vision
    "embedding_provider": "bge_m3",
    # 火山方舟 API Key，Chat 和 Embedding 两个接口共用此 Key 做身份认证
    "api_key": "ark-6d059414-9147-47b1-a004-8cc930a2e91d-c527b",
    # Embedding 接入点 ID，绑定 doubao-embedding-vision 模型（仅 doubao 模式使用）
    "embedding_endpoint_id": "ep-20260429184857-sxcxs",
    # Chat 接入点 ID，绑定 Doubao-Seed-2.0-pro 模型，用于生成场景强化摘要
    "chat_endpoint_id": "ep-20260429185101-4pqz5",
    "similarity_threshold": 0.6
}

# --------------------------
# 固定接口地址（vision 多模态专用）
# --------------------------
EMBEDDING_URL = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

# --------------------------
# 1. 场景强化摘要生成（核心功能）
# --------------------------
def generate_scene_enhanced_summary(article_title: str, article_content: str) -> str:
    prompt = f"""
    请你作为税务政策专家，从以下税务文章中提炼【场景强化核心摘要】，严格遵守以下约束：
    1. 必须包含：适用纳税主体（如居民企业/小微企业）、限定地区（如西部地区）、行业范围（如鼓励类产业）、特殊税率（如7.5%/15%）、优惠条件（如主营业务占比）；
    2. 重点抓取：非常规税率、区域性优惠、行业专属政策、叠加优惠规则、比例要求（如占比60%以上）；
    3. 禁止泛泛而谈，必须保留具体数字、限定词、政策专属名词；
    4. 字数控制在80-120字，仅输出摘要文本，无额外解释、无标题、无多余符号；
    5. 若涉及企业所得税税率优惠，必须明确税率数值及适用场景。

    文章标题：{article_title}
    文章内容：{article_content[:1000]}
    """

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CONFIG['api_key']}"
    }
    payload = {
        "model": CONFIG["chat_endpoint_id"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }

    try:
        response = requests.post(CHAT_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"摘要生成异常：{str(e)}")
        return f"【{article_title}】企业所得税相关政策，适用居民企业，涉及税率优惠与适用条件"

# --------------------------
# 2. 向量生成函数（支持 BGE-M3 本地 / doubao API）
# --------------------------
# BGE-M3 模型懒加载，首次调用时初始化
_bge_model = None

def _get_bge_model():
    global _bge_model
    if _bge_model is None:
        from sentence_transformers import SentenceTransformer
        import os
        model_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bge_m3_cache", "Xorbits", "bge-m3")
        print(f"正在加载 BGE-M3 模型（本地路径: {model_path}）...")
        _bge_model = SentenceTransformer(model_path)
        print("BGE-M3 模型加载完成")
    return _bge_model

def generate_embedding(text: str) -> List[float]:
    provider = CONFIG["embedding_provider"]

    if provider == "bge_m3":
        model = _get_bge_model()
        emb = model.encode(text[:500], normalize_embeddings=True)
        return emb.tolist()

    elif provider == "doubao":
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CONFIG['api_key']}"
        }
        payload = {
            "model": CONFIG["embedding_endpoint_id"],
            "input": [{"type": "text", "text": text[:500]}]
        }
        try:
            response = requests.post(EMBEDDING_URL, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()["data"]["embedding"]
        except Exception as e:
            print(f"向量生成异常：{str(e)}")
            np.random.seed(len(text))
            return np.random.rand(1024).tolist()

    else:
        raise ValueError(f"未知的 embedding_provider: {provider}，请使用 bge_m3 或 doubao")

# --------------------------
# 3. 文章数据预处理
# --------------------------
def load_articles(file_path: str = "问题1核心文章.xlsx") -> List[Dict]:
    df = pd.read_excel(file_path)
    articles = []
    for idx, row in df.iterrows():
        title = str(row["标题"]).strip()
        content = str(row["正文"]).strip()
        normal_summary = content[:200] + "..." if len(content) > 200 else content
        articles.append({
            "id": idx + 1,
            "title": title,
            "content": content,
            "normal_summary": normal_summary
        })
    print(f"成功加载 {len(articles)} 篇文章")
    return articles

# --------------------------
# 4. 批量生成双向量
# --------------------------
def process_articles(articles: List[Dict]) -> List[Dict]:
    processed = []
    for art in articles:
        print(f"处理文章 {art['id']}：{art['title']}")
        scene_summary = generate_scene_enhanced_summary(art["title"], art["content"])
        normal_emb = generate_embedding(art["normal_summary"])
        scene_emb = generate_embedding(scene_summary)
        processed.append({
            "id": art["id"],
            "title": art["title"],
            "normal_summary": art["normal_summary"],
            "scene_enhanced_summary": scene_summary,
            "normal_embedding": normal_emb,
            "scene_enhanced_embedding": scene_emb
        })
    return processed

# --------------------------
# 5. 检索排序函数
# --------------------------
def retrieve_articles(test_question: str, processed_articles: List[Dict], use_scene: bool = True) -> List[Dict]:
    question_emb = generate_embedding(test_question)
    results = []
    for art in processed_articles:
        emb = art["scene_enhanced_embedding"] if use_scene else art["normal_embedding"]
        similarity = cosine_similarity(
            np.array(question_emb).reshape(1, -1),
            np.array(emb).reshape(1, -1)
        )[0][0]
        results.append({
            "title": art["title"],
            "similarity": round(float(similarity), 4),
            "above_threshold": "是" if similarity >= CONFIG["similarity_threshold"] else "否",
            "vector_type": "场景强化向量" if use_scene else "普通向量",
            "scene_summary": art["scene_enhanced_summary"]
        })
    sorted_results = sorted(results, key=lambda x: x["similarity"], reverse=True)
    for i, item in enumerate(sorted_results):
        item["rank"] = i + 1
    return sorted_results

# --------------------------
# 6. 主函数（一键运行）
# --------------------------
def main():
    import sys, os
    from datetime import datetime

    if len(sys.argv) < 2:
        print("用法: python app/scene_vector.py <Excel文件路径>")
        print("示例: python app/scene_vector.py 问题1核心文章.xlsx")
        sys.exit(1)

    file_path = sys.argv[1]
    articles = load_articles(file_path)
    processed_articles = process_articles(articles)

    test_question = "现行企业所得税法下，居民企业什么情况下取得的所得可能适用7.5%税率？"
    print(f"\n测试问题：{test_question}")

    print("\n【普通向量检索结果】")
    normal_result = retrieve_articles(test_question, processed_articles, use_scene=False)
    for item in normal_result:
        print(f"排名{item['rank']} | 相似度{item['similarity']} | {item['title']}")

    print("\n【场景强化向量检索结果】")
    scene_result = retrieve_articles(test_question, processed_articles, use_scene=True)
    for item in scene_result:
        print(f"排名{item['rank']} | 相似度{item['similarity']} | {item['title']}")

    # 输出结果到 Excel
    output_dir = os.path.join("outputs", "场景向量")
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"检索结果_{timestamp}.xlsx")

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Sheet1: 场景强化摘要
        summary_data = [{
            "ID": art["id"],
            "标题": art["title"],
            "普通摘要": art["normal_summary"],
            "场景强化摘要": art["scene_enhanced_summary"]
        } for art in processed_articles]
        pd.DataFrame(summary_data).to_excel(writer, sheet_name="场景强化摘要", index=False)

        # Sheet2: 普通向量检索结果
        normal_df = pd.DataFrame(normal_result)
        normal_df.insert(0, "相似度阈值", CONFIG["similarity_threshold"])
        normal_df.to_excel(writer, sheet_name="普通向量检索", index=False)

        # Sheet3: 场景强化向量检索结果
        scene_df = pd.DataFrame(scene_result)
        scene_df.insert(0, "相似度阈值", CONFIG["similarity_threshold"])
        scene_df.to_excel(writer, sheet_name="场景强化检索", index=False)

    print(f"\n结果已保存到: {output_path}")

if __name__ == "__main__":
    main()
