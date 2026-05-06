#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
回答稳定性模块 - 四个Skill并行处理 + 动态提示词组装
解决同一问题+所有文章下AI回答不稳定的问题
"""

import os
import json
import queue
import urllib.request
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, request as flask_request, jsonify, Response
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Alignment, Font, Border, Side

from app.app import (
    get_config, logger,
    ROOT_DIR, allowed_file, request_api, parse_response,
    is_confirmation_question, is_incomplete_answer
)

stability_bp = Blueprint('answer_stability', __name__)

UPLOAD_FOLDER = os.path.join(ROOT_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(ROOT_DIR, 'outputs')


def _get_articles_full(question: str, max_rounds: int = 12) -> list:
    """多轮对话获取文章，返回每轮内容的列表（不丢弃中间轮次的文章原文）"""
    session_id = ""
    current_message = question
    all_parts = []

    for round_i in range(max_rounds):
        try:
            api_response, session_id = request_api(current_message, session_id)
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


# ===================== Combined Skill Prompt =====================

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

FINAL_ANSWER_SYSTEM = "你是一个税务问答专家。请严格按以下结构和约束回答问题。"


# ===================== Helper Functions =====================

def _sse(data_dict):
    return f"data: {json.dumps(data_dict, ensure_ascii=False)}\n\n"


def _log(msg):
    """统一日志：同时输出到logger和VSCode控制台"""
    logger.info(msg)
    print(msg, flush=True)


def _call_llm(system_prompt, user_prompt, temperature=0.01, max_tokens=2000, timeout=300, max_retries=3):
    """Call LLM with system prompt and temperature control"""
    base_url = get_config('llm_base_url', '').rstrip('/')
    key = get_config('llm_api_key', '')
    model = get_config('model', '')

    if not base_url or not key or not model:
        raise ValueError("LLM API未配置（llm_base_url/llm_api_key/model）")

    url = base_url + '/v1/chat/completions'

    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': system_prompt})
    messages.append({'role': 'user', 'content': user_prompt})

    body = {'model': model, 'messages': messages, 'temperature': temperature}
    if max_tokens > 0:
        body['max_tokens'] = max_tokens
    data = json.dumps(body).encode('utf-8')

    for attempt in range(1, max_retries + 1):
        result = [None, None]

        def _do():
            try:
                req = urllib.request.Request(url, data=data, headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {key}'
                })
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode('utf-8')
                    r = json.loads(raw)
                    if 'error' in r:
                        _log(f"[stability] _call_llm API返回错误: {r['error']}")
                        result[1] = RuntimeError(str(r['error']))
                    elif 'choices' in r and r['choices']:
                        content = r['choices'][0]['message']['content']
                        _log(f"[stability] _call_llm 模型={model} temperature={temperature} 返回长度={len(content) if content else 0}")
                        result[0] = content
                    else:
                        _log(f"[stability] _call_llm API返回格式异常: {raw[:300]}")
                        result[1] = RuntimeError(f"API返回格式异常: {str(r)[:200]}")
            except Exception as e:
                _log(f"[stability] _call_llm 请求异常: {e}")
                result[1] = e

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=timeout + 30)

        if t.is_alive():
            if attempt >= max_retries:
                raise TimeoutError(f"LLM调用超时（>{timeout + 30}秒）")
            continue

        if result[1] is not None:
            if attempt >= max_retries:
                raise result[1]
            time.sleep(min(attempt * 2, 10))
            continue

        if result[0] is not None:
            return result[0]

    raise RuntimeError("LLM调用失败")


# ===================== BGE-M3 Embedding =====================

_bge_model = None

def _get_bge_model():
    """懒加载BGE-M3本地模型"""
    global _bge_model
    if _bge_model is None:
        from sentence_transformers import SentenceTransformer
        model_path = os.path.join(ROOT_DIR, "bge_m3_cache", "Xorbits", "bge-m3")
        _log(f"[stability] 正在加载BGE-M3模型（路径: {model_path}）...")
        _bge_model = SentenceTransformer(model_path)
        _log("[stability] BGE-M3模型加载完成")
    return _bge_model


def _generate_embedding(text):
    """用BGE-M3生成文本向量"""
    model = _get_bge_model()
    emb = model.encode(text[:500], normalize_embeddings=True)
    return emb.tolist()


# ===================== Article Grouping =====================

def _group_articles(article_parts, max_chars=None, sim_threshold=None):
    """按BGE-M3向量相似度对文章分组

    返回 list[list[str]]，每个内层列表是一组文章文本
    """
    import numpy as np

    max_chars = max_chars or get_config('group_max_chars', 12000)
    sim_threshold = sim_threshold or get_config('group_sim_threshold', 0.6)

    if len(article_parts) <= 1:
        return [article_parts] if article_parts else []

    # 为每篇文章生成向量
    article_data = []
    for i, content in enumerate(article_parts):
        vec = _generate_embedding(content)
        article_data.append({'content': content, 'vec': vec, 'len': len(content)})
    _log(f"[stability] 向量生成完成，共{len(article_data)}篇文章")

    groups = []  # list of {'articles': [...], 'vecs': [...], 'total_len': int}

    for art in article_data:
        # 单篇超长：独立成组
        if art['len'] > max_chars:
            groups.append({'articles': [art], 'vecs': [art['vec']], 'total_len': art['len']})
            continue

        placed = False
        for group in groups:
            # 字符数检查
            if group['total_len'] + art['len'] > max_chars:
                continue
            # 相似度检查：与组内已有文章的平均余弦相似度
            sims = [float(np.dot(art['vec'], v)) for v in group['vecs']]
            avg_sim = sum(sims) / len(sims)
            if avg_sim >= sim_threshold:
                group['articles'].append(art)
                group['vecs'].append(art['vec'])
                group['total_len'] += art['len']
                placed = True
                break

        if not placed:
            groups.append(
                {'articles': [art], 'vecs': [art['vec']], 'total_len': art['len']}
            )

    # 提取文章文本
    result = [[a['content'] for a in g['articles']] for g in groups]
    return result


# ===================== Merge Functions =====================

def _merge_condition_pairs(all_pairs, sim_threshold=None):
    """合并条件-结论对：语义相似度>=阈值则合并article_ids"""
    import numpy as np

    if not all_pairs:
        return []

    sim_threshold = sim_threshold or get_config('merge_cc_sim_threshold', 0.95)
    merged = []
    used_indices = set()

    for i, pair in enumerate(all_pairs):
        if i in used_indices:
            continue
        current = dict(pair)
        ids = list(current.get('article_ids', current.get('article_id', [])))
        if isinstance(ids, str):
            ids = [ids]

        for j in range(i + 1, len(all_pairs)):
            if j in used_indices:
                continue
            other = all_pairs[j]
            # 计算条件+结论的语义相似度
            text_i = current.get('condition', '') + current.get('conclusion', '')
            text_j = other.get('condition', '') + other.get('conclusion', '')
            if text_i and text_j:
                vec_i = _generate_embedding(text_i)
                vec_j = _generate_embedding(text_j)
                sim = float(np.dot(vec_i, vec_j))
                if sim >= sim_threshold:
                    other_ids = other.get('article_ids', other.get('article_id', []))
                    if isinstance(other_ids, str):
                        other_ids = [other_ids]
                    ids.extend([x for x in other_ids if x not in ids])
                    used_indices.add(j)

        current['article_ids'] = ids
        if 'article_id' in current:
            del current['article_id']
        merged.append(current)

    return merged


def _merge_time_constraints(all_constraints):
    """合并时间约束：同policy+同type+同条件则合并article_ids"""
    if not all_constraints:
        return []

    merged = []
    seen = {}
    for tc in all_constraints:
        key = (tc.get('policy_name', ''), tc.get('constraint_type', ''), tc.get('condition', ''))
        ids = list(tc.get('article_ids', tc.get('article_id', [])))
        if isinstance(ids, str):
            ids = [ids]
        if key in seen:
            idx = seen[key]
            existing_ids = merged[idx].get('article_ids', [])
            merged[idx]['article_ids'] = list(set(existing_ids + ids))
        else:
            seen[key] = len(merged)
            item = dict(tc)
            item['article_ids'] = ids
            if 'article_id' in item:
                del item['article_id']
            merged.append(item)

    return merged


def _parse_json_response(text):
    """Extract JSON from LLM response"""
    if not text:
        return None
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for start_char, end_char in [('[', ']'), ('{', '}')]:
        start = text.find(start_char)
        end = text.rfind(end_char) + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except Exception:
                pass
    return None


def _parse_articles_from_text(text):
    """将C列文章内容按NTPSID标记拆分为逐篇文章列表

    每篇文章格式：
      ## {序号}. NTPSID: {数字id}
      ...文章正文...
      ---
    """
    import re
    if not text or not text.strip():
        return []

    # 按 --- 分割（精确匹配单独一行的三个连字符）
    separator_pattern = re.compile(r'^---$', re.MULTILINE)
    segments = separator_pattern.split(text)

    # 用 NTPSID 标记识别每篇文章
    ntpsid_pattern = re.compile(r'^##\s*\d+\.\s*NTPSID:\s*\d+', re.MULTILINE)
    articles = []

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        # 找到所有NTPSID标记位置
        matches = list(ntpsid_pattern.finditer(segment))
        if not matches:
            # 没有NTPSID标记，整段作为一篇文章
            articles.append(segment)
            continue
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
            article_text = segment[start:end].strip()
            if article_text:
                articles.append(article_text)

    # 如果没拆出任何文章，返回原文作为单篇
    if not articles:
        articles = [text.strip()]

    return articles


def _dedupe_strings(items):
    """去重字符串列表，保持顺序"""
    seen = set()
    result = []
    for item in items:
        s = str(item) if not isinstance(item, str) else item
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


# ===================== Assertion Validation (C1-C7) =====================

def _validate_assertions(assertions):
    """Validate and clean concept relation assertions (rules C1-C7)"""
    if not assertions or not isinstance(assertions, list):
        return []

    cleaned = [dict(a) for a in assertions if isinstance(a, dict)]

    # C1: Remove self-referential
    cleaned = [a for a in cleaned if a.get('entity_a', '') != a.get('entity_b', '')]

    # C2: Deduplicate, merge evidence
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

    # C3: synonym + mutually_exclusive → delete mutually_exclusive
    pair_rels = {}
    for a in cleaned:
        k = (a.get('entity_a', ''), a.get('entity_b', ''))
        pair_rels.setdefault(k, set()).add(a.get('relation_type', ''))

    conflict_c3 = {k for k, rels in pair_rels.items() if 'synonym' in rels and 'mutually_exclusive' in rels}
    if conflict_c3:
        cleaned = [a for a in cleaned if not (
            (a.get('entity_a', ''), a.get('entity_b', '')) in conflict_c3
            and a.get('relation_type', '') == 'mutually_exclusive'
        )]

    # C4: synonym + hypernym → delete hypernym
    conflict_c4 = {k for k, rels in pair_rels.items() if 'synonym' in rels and 'hypernym' in rels}
    if conflict_c4:
        cleaned = [a for a in cleaned if not (
            (a.get('entity_a', ''), a.get('entity_b', '')) in conflict_c4
            and a.get('relation_type', '') == 'hypernym'
        )]

    # C5: Symmetric mutually_exclusive
    me_pairs = {(a.get('entity_a', ''), a.get('entity_b', ''))
                for a in cleaned if a.get('relation_type', '') == 'mutually_exclusive'}
    existing = {(a.get('entity_a', ''), a.get('entity_b', '')) for a in cleaned}
    for ea, eb in me_pairs:
        if (eb, ea) not in existing:
            cleaned.append({
                'entity_a': eb, 'entity_b': ea,
                'relation_type': 'mutually_exclusive',
                'evidence': 'derived: symmetric complement',
                'derived': True
            })

    # C6: Evidence entity matching
    validated = []
    for a in cleaned:
        if a.get('derived'):
            validated.append(a)
            continue
        ev = a.get('evidence', '')
        ea = a.get('entity_a', '')
        eb = a.get('entity_b', '')
        if (ea and ea in ev) or (eb and eb in ev):
            validated.append(a)
    cleaned = validated

    # C7: Evidence length limit
    for a in cleaned:
        if len(a.get('evidence', '')) > 500:
            a['evidence'] = a['evidence'][:200] + '...'

    return cleaned


def _convert_constraints_to_text(cleaned_assertions):
    """Convert cleaned assertions to natural language constraint texts"""
    texts = []
    LQ = '“'  # left double quotation mark
    RQ = '”'  # right double quotation mark
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


def _assemble_final_prompt(user_query, condition_pairs, scene_enum, constraint_texts, time_constraints):
    """Assemble the final prompt from all skill products"""

    # Scene enumeration
    if isinstance(scene_enum, list) and scene_enum:
        scene_list = "\n".join(f"- {s}" if isinstance(s, str) else f"- {s}" for s in scene_enum)
    else:
        scene_list = "未提取到政策场景"

    # Constraint texts
    constraints = "\n".join(f"- {t}" for t in constraint_texts) if constraint_texts else "无特殊概念约束"

    # Time constraints
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

    # Condition-conclusion pairs
    if isinstance(condition_pairs, list) and condition_pairs:
        cc_lines = []
        for i, cc in enumerate(condition_pairs, 1):
            cond = cc.get('condition', '')
            conc = cc.get('conclusion', '')
            aids = cc.get('article_ids', cc.get('article_id', ''))
            if isinstance(aids, list):
                aids = ', '.join(str(a) for a in aids)
            cc_lines.append(f"- 条件：{cond}\n  结论：{conc}\n  来源：{aids}")
        cc_text = "\n".join(cc_lines)
    else:
        cc_text = "未抽取到条件-结论对"

    return f"""你是一个税务问答专家。请严格按以下结构和约束回答问题。

## 用户问题
{user_query}

## 必须检查的政策场景（不要遗漏任何一项）
{scene_list}

## 逻辑约束（必须遵守）
{constraints}

## 时间适用性约束
{time_text}

## 可用的条件-结论对（推理依据）
{cc_text}

## 回答步骤
1. 对【必须检查的政策场景】中的每一个场景，判断是否适用，并引用【可用的条件-结论对】中的对应条目。
2. 检查政策之间是否存在互斥或叠加限制，依据【逻辑约束】。
3. 最终输出必须：
   - 列出所有适用的场景及其结论。
   - 如果有多种适用情况，明确说明它们是否可以同时享受，若不能则指出优先级。
   - 给出最终的汇总答案。

请现在开始回答。"""


# ===================== Routes =====================

@stability_bp.route('/stability_upload', methods=['POST'])
def stability_upload():
    """上传Excel，解析A列(问题)、B列(提示词)、C列(文章内容)"""
    file = flask_request.files.get('file')
    if not file or not allowed_file(file.filename):
        return jsonify({'error': '请上传 .xlsx 或 .xls 文件'}), 400

    import re
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', file.filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{safe_name}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        wb = load_workbook(filepath, read_only=True, data_only=True)
        ws = wb.active
        questions = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            question = str(row[0]).strip() if len(row) > 0 and row[0] else ''
            prompt = str(row[1]).strip() if len(row) > 1 and row[1] else ''
            articles_text = str(row[2]).strip() if len(row) > 2 and row[2] else ''
            if not question or question == 'None':
                continue
            has_articles = bool(articles_text)
            article_count = 0
            if has_articles:
                parsed = _parse_articles_from_text(articles_text)
                article_count = len(parsed)
            questions.append({
                'row': row_idx,
                'question': question,
                'prompt': prompt,
                'has_prompt': bool(prompt),
                'has_articles': has_articles,
                'article_count': article_count,
                'articles_preview': articles_text[:200] + '...' if len(articles_text) > 200 else articles_text
            })
        wb.close()
    except Exception as e:
        return jsonify({'error': f'解析Excel失败: {e}'}), 400

    if not questions:
        return jsonify({'error': '未找到有效数据（A列需有问题）'}), 400

    _log(f"[stability] Excel解析完成: {filename}, 共{len(questions)}题")
    prompt_count = 0
    articles_count = 0
    for q in questions:
        _log(f"[stability]   行{q['row']}: 问题={q['question'][:80]}{'...' if len(q['question']) > 80 else ''}")
        if q['has_prompt']:
            prompt_count += 1
        if q['has_articles']:
            articles_count += 1
            _log(f"[stability]     C列文章: {q['article_count']}篇")
    _log(f"[stability] 共读取{len(questions)}个问题，其中{prompt_count}个有提示词，{articles_count}个有C列文章")
    
    return jsonify({
        'filename': filename,
        'question_count': len(questions),
        'questions': questions
    })


@stability_bp.route('/stability_process', methods=['POST'])
def stability_process():
    """SSE流式处理：获取文章→四Skill并行→校验→组装→生成最终回答"""
    data = flask_request.get_json()
    filename = data.get('filename', '')
    thread_count = max(1, int(data.get('thread_count', 2)))

    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': '文件不存在，请重新上传'}), 400

    try:
        wb = load_workbook(filepath, read_only=True, data_only=True)
        ws = wb.active
        questions = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            question = str(row[0]).strip() if len(row) > 0 and row[0] else ''
            prompt = str(row[1]).strip() if len(row) > 1 and row[1] else ''
            articles_text = str(row[2]).strip() if len(row) > 2 and row[2] else ''
            if not question or question == 'None':
                continue
            questions.append((row_idx, question, prompt, articles_text))
        wb.close()
    except Exception as e:
        return jsonify({'error': f'解析Excel失败: {e}'}), 400

    total = len(questions)
    if total == 0:
        return jsonify({'error': '未找到有效问题'}), 400

    model_name = get_config('model', '')
    _log(f"[stability] 开始处理 {total}题 | {thread_count}线程 | 模型={model_name}")

    def generate():
        all_results = []

        for q_idx, (row_num, question, kb_prompt, c_articles) in enumerate(questions):
            _log(f"[stability] ===== 第{q_idx + 1}/{total}题 (行{row_num}) =====")
            yield _sse({'type': 'question_start', 'question_idx': q_idx, 'total_questions': total,
                        'question': question, 'prompt': kb_prompt})

            products = {
                'condition_pairs': [],
                'scene_enum': [],
                'assertions_raw': [],
                'assertions_cleaned': [],
                'constraint_texts': [],
                'time_constraints': [],
                'articles_text': '',
                'final_prompt': '',
                'final_answer': ''
            }

            # ---- Step 1: Get articles ----
            if c_articles:
                # C列有文章内容，直接拆分使用，跳过知识库API
                _log(f"[stability] row={row_num} 从Excel C列读取文章（长度={len(c_articles)}）")
                yield _sse({'type': 'step_start', 'question_idx': q_idx,
                            'step_id': 'articles', 'step_label': '读取文章内容(Excel C列)',
                            'system_prompt': '(从Excel C列直接读取)', 'user_prompt': c_articles[:500]})
                article_parts = _parse_articles_from_text(c_articles)
                articles_full_text = '\n\n'.join(article_parts)
                products['articles_text'] = articles_full_text
                for pi, part in enumerate(article_parts):
                    _log(f"[stability] row={row_num} 文章{pi+1}（长度={len(part)}）: {part[:200]}{'...' if len(part) > 200 else ''}")
                _log(f"[stability] row={row_num} 共{len(article_parts)}篇文章，总长度={len(articles_full_text)}")
                yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                            'step_id': 'articles', 'step_label': '读取文章内容(Excel C列)',
                            'response': articles_full_text})
            else:
                # C列为空，从知识库API获取
                yield _sse({'type': 'step_start', 'question_idx': q_idx,
                            'step_id': 'articles', 'step_label': '获取文章内容(知识库API)',
                            'system_prompt': '(通过5007知识库API获取)', 'user_prompt': kb_prompt or question})
                try:
                    article_parts = _get_articles_full(kb_prompt or question)
                    articles_full_text = '\n\n'.join(article_parts)
                    products['articles_text'] = articles_full_text
                    for pi, part in enumerate(article_parts):
                        _log(f"[stability] row={row_num} 文章{pi+1}（长度={len(part)}）: {part[:200]}{'...' if len(part) > 200 else ''}")
                    _log(f"[stability] row={row_num} 共获取{len(article_parts)}篇文章，总长度={len(articles_full_text)}")
                    yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                                'step_id': 'articles', 'step_label': '获取文章内容(知识库API)',
                                'response': articles_full_text})
                except Exception as e:
                    _log(f"[stability] row={row_num} 文章获取失败: {e}")
                    yield _sse({'type': 'step_error', 'question_idx': q_idx,
                                'step_id': 'articles', 'step_label': '获取文章内容(知识库API)',
                                'error': str(e)})
                    article_parts = [kb_prompt or question]
                    articles_full_text = article_parts[0]
                    products['articles_text'] = articles_full_text

            # ---- Step 2: Group articles + Combined Skill ----
            # 2a: 分组（基于BGE-M3向量相似度）
            max_chars = get_config('group_max_chars', 12000)
            sim_threshold = get_config('group_sim_threshold', 0.6)
            _log(f"[stability] row={row_num} 开始分组（max_chars={max_chars}, sim_threshold={sim_threshold}）")
            yield _sse({'type': 'step_start', 'question_idx': q_idx,
                        'step_id': 'grouping', 'step_label': '文章分组(向量相似度)',
                        'system_prompt': f'(BGE-M3向量+余弦相似度>={sim_threshold})',
                        'user_prompt': f'{len(article_parts)}篇文章'})

            try:
                groups = _group_articles(article_parts, max_chars, sim_threshold)
            except Exception as e:
                _log(f"[stability] row={row_num} 分组失败，回退为逐篇处理: {e}")
                groups = [[art] for art in article_parts]

            group_info = ' | '.join(f"组{i+1}:{len(g)}篇" for i, g in enumerate(groups))
            _log(f"[stability] row={row_num} 分组完成: {len(groups)}组 — {group_info}")
            for gi, g in enumerate(groups):
                import re
                art_ids = []
                for art in g:
                    m = re.search(r'NTPSID:\s*(\d+)', art)
                    art_ids.append(f"ART_{m.group(1)}" if m else f"(未知)")
                total_chars = sum(len(a) for a in g)
                _log(f"[stability]   组{gi+1}: 文章={art_ids}, 总字符数={total_chars}")
            yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                        'step_id': 'grouping', 'step_label': '文章分组(向量相似度)',
                        'response': f"分为{len(groups)}组: {group_info}"})

            # 2b: 每组调用合并Skill（并行）
            task_queue = queue.Queue()

            def _run_combined(group_idx, group_articles):
                label = f"合并Skill(组{group_idx+1})"
                sid = f'combined_group{group_idx+1}'
                try:
                    # 为每篇文章提取NTPSID作为ID
                    import re
                    arts_text_parts = []
                    group_art_ids = []
                    for ai, art in enumerate(group_articles):
                        ntpsid_match = re.search(r'NTPSID:\s*(\d+)', art)
                        art_id = f"ART_{ntpsid_match.group(1)}" if ntpsid_match else f"ART_{row_num}_G{group_idx+1}_P{ai+1}"
                        group_art_ids.append(art_id)
                        arts_text_parts.append(f"### 文章ID: {art_id}\n{art}")
                    combined_text = '\n\n'.join(arts_text_parts)
                    user_prompt = f"用户问题：{question}\n\n文章列表：\n{combined_text}"
                    resp = _call_llm(COMBINED_SKILL_SYSTEM, user_prompt,
                                     temperature=0, max_tokens=0, timeout=600)
                    parsed = _parse_json_response(resp)
                    if parsed is None and resp:
                        _log(f"[stability] row={row_num} {label} JSON解析失败，原始响应前500字: {resp[:500]}")
                    task_queue.put((sid, label, resp, parsed, None, group_art_ids))
                except Exception as e:
                    task_queue.put((sid, label, None, None, str(e), []))

            # Send start events + submit tasks
            for gi, group in enumerate(groups):
                sid = f'combined_group{gi+1}'
                label = f"合并Skill(组{gi+1})"
                yield _sse({'type': 'step_start', 'question_idx': q_idx,
                            'step_id': sid, 'step_label': label,
                            'system_prompt': COMBINED_SKILL_SYSTEM[:300],
                            'user_prompt': f'组{gi+1}: {len(group)}篇文章'})

            with ThreadPoolExecutor(max_workers=min(thread_count, len(groups))) as executor:
                for gi, group in enumerate(groups):
                    executor.submit(_run_combined, gi, group)

                all_cc_pairs = []
                all_scenes = []
                all_relations = []
                all_time_constraints = []
                completed = 0

                while completed < len(groups):
                    sid, label, resp, parsed, error, group_art_ids = task_queue.get()
                    completed += 1
                    if error:
                        _log(f"[stability] row={row_num} {label}失败: {error}")
                        yield _sse({'type': 'step_error', 'question_idx': q_idx,
                                    'step_id': sid, 'step_label': label, 'error': error})
                    else:
                        _log(f"[stability] row={row_num} {label}完成，原始响应:\n{resp}")
                        if parsed and isinstance(parsed, dict):
                            cc = parsed.get('condition_conclusion_pairs', [])
                            scenes = parsed.get('policy_scenes', [])
                            rels = parsed.get('concept_relations', [])
                            tcs = parsed.get('time_constraints', [])
                            # 概念关系字段名映射
                            for item in rels:
                                if isinstance(item, dict):
                                    item.setdefault('entity_a', item.pop('concept_A', ''))
                                    item.setdefault('entity_b', item.pop('concept_B', ''))
                                    item.setdefault('relation_type', item.pop('relation', ''))
                            # 规范化article_ids
                            for item in cc + tcs + rels:
                                if isinstance(item, dict):
                                    aid = item.pop('article_id', None)
                                    if aid and 'article_ids' not in item:
                                        item['article_ids'] = [aid] if isinstance(aid, str) else aid
                                    # 如果仍无article_ids，用本组的文章ID回填
                                    if not item.get('article_ids'):
                                        item['article_ids'] = list(group_art_ids)
                            all_cc_pairs.extend(cc if isinstance(cc, list) else [])
                            all_scenes.extend(scenes if isinstance(scenes, list) else [])
                            all_relations.extend(rels if isinstance(rels, list) else [])
                            all_time_constraints.extend(tcs if isinstance(tcs, list) else [])
                            _log(f"[stability] row={row_num} {label} → 条件-结论={len(cc)}, "
                                 f"场景={len(scenes)}, 断言={len(rels)}, 时间={len(tcs)}")
                        yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                                    'step_id': sid, 'step_label': label,
                                    'response': resp, 'parsed': parsed})

            # 2c: 跨组合并去重
            _log(f"[stability] row={row_num} 开始跨组合并（条件-结论={len(all_cc_pairs)}, "
                 f"场景={len(all_scenes)}, 断言={len(all_relations)}, "
                 f"时间约束={len(all_time_constraints)}）")

            products['condition_pairs'] = _merge_condition_pairs(all_cc_pairs)
            products['scene_enum'] = _dedupe_strings(all_scenes)
            products['assertions_raw'] = all_relations
            products['time_constraints'] = _merge_time_constraints(all_time_constraints)

            _log(f"[stability] row={row_num} 合并结果: 条件-结论={len(products['condition_pairs'])}条, "
                 f"场景={len(products['scene_enum'])}个, 断言={len(products['assertions_raw'])}条, "
                 f"时间约束={len(products['time_constraints'])}条")

            # ---- Step 3: Validate assertions ----
            raw_cc_count = len(products['condition_pairs'])
            raw_scene_count = len(products['scene_enum'])
            raw_tc_count = len(products['time_constraints'])

            raw_count = len(products['assertions_raw'])
            cleaned = _validate_assertions(products['assertions_raw'])
            products['assertions_cleaned'] = cleaned
            constraint_texts = _convert_constraints_to_text(cleaned)
            products['constraint_texts'] = constraint_texts

            _log(f"[stability] row={row_num} 断言校验: {raw_count}→{len(cleaned)}")
            _log(f"[stability] row={row_num} 条件-结论对(合并后): {raw_cc_count}条")
            _log(f"[stability] row={row_num} 政策场景(去重后): {raw_scene_count}个")
            _log(f"[stability] row={row_num} 时间约束(合并后): {raw_tc_count}条")
            yield _sse({'type': 'validation', 'question_idx': q_idx,
                        'original_count': raw_count, 'cleaned_count': len(cleaned),
                        'removed_count': raw_count - len(cleaned),
                        'details': f"原始{raw_count}条 → 清洗后{len(cleaned)}条"})

            # ---- Step 4: Assemble final prompt ----
            final_prompt = _assemble_final_prompt(
                question,
                products['condition_pairs'],
                products['scene_enum'],
                constraint_texts,
                products['time_constraints']
            )
            products['final_prompt'] = final_prompt
            _log(f"[stability] row={row_num} 最终提示词组装完成（长度={len(final_prompt)}）")
            yield _sse({'type': 'final_prompt', 'question_idx': q_idx, 'prompt': final_prompt})

            # ---- Step 5: Generate final answer ----
            yield _sse({'type': 'step_start', 'question_idx': q_idx,
                        'step_id': 'final_answer', 'step_label': '生成最终回答',
                        'system_prompt': FINAL_ANSWER_SYSTEM, 'user_prompt': final_prompt})
            # # 暂时注释掉最终回答生成，先输出组装后的提示词
            # try:
            #     final_answer = _call_llm(FINAL_ANSWER_SYSTEM, final_prompt,
            #                              temperature=0.3, max_tokens=4000, timeout=600)
            #     products['final_answer'] = final_answer
            #     logger.info(f"[stability] row={row_num} 最终回答完成（长度={len(final_answer)}）")
            #     yield _sse({'type': 'step_complete', 'question_idx': q_idx,
            #                 'step_id': 'final_answer', 'step_label': '生成最终回答',
            #                 'response': final_answer})
            # except Exception as e:
            #     fallback = "系统暂无法回答此问题，请稍后重试或咨询税务专员。"
            #     products['final_answer'] = fallback
            #     logger.error(f"[stability] row={row_num} 最终回答生成失败: {e}")
            #     yield _sse({'type': 'step_error', 'question_idx': q_idx,
            #                 'step_id': 'final_answer', 'step_label': '生成最终回答',
            #                 'error': str(e)})
            products['final_answer'] = "（最终回答生成已禁用，请查看上方最终组装的提示词）"
            yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                        'step_id': 'final_answer', 'step_label': '生成最终回答',
                        'response': products['final_answer']})

            yield _sse({'type': 'question_complete', 'question_idx': q_idx,
                        'answer': products['final_answer']})

            all_results.append({
                'row': row_num,
                'question': question,
                'prompt': kb_prompt,
                'articles_text': products['articles_text'],
                'raw_condition_pairs': all_cc_pairs,
                'condition_pairs': products['condition_pairs'],
                'raw_scene_enum': all_scenes,
                'scene_enum': products['scene_enum'],
                'raw_assertions': all_relations,
                'assertions_cleaned': products['assertions_cleaned'],
                'constraint_texts': products['constraint_texts'],
                'raw_time_constraints': all_time_constraints,
                'time_constraints': products['time_constraints'],
                'final_prompt': products['final_prompt'],
                'final_answer': products['final_answer']
            })

        # Save results
        try:
            stability_output_dir = os.path.join(OUTPUT_FOLDER, '回答稳定性结果')
            os.makedirs(stability_output_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"回答稳定性结果_{ts}.xlsx"
            output_path = os.path.join(stability_output_dir, output_filename)
            _save_results(all_results, output_path)
            _log(f"[stability] 结果已保存: {output_path}")
        except Exception as e:
            _log(f"[stability] 保存结果失败: {e}")
            output_filename = ""

        _log(f"[stability] ===== 全部完成 ({total}题) =====")
        yield _sse({'type': 'complete', 'total_questions': total,
                    'output_filename': f'回答稳定性结果/{output_filename}'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _save_results(results, output_path):
    """Save results to Excel with pre/post processing comparison"""
    wb = Workbook()
    ws = wb.active
    ws.title = '回答稳定性结果'

    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    headers = [
        ('问题', 50),
        ('提示词(B列)', 40),
        ('文章内容', 80),
        ('条件-结论对(处理前)', 60),
        ('条件-结论对(处理后)', 60),
        ('政策场景(处理前)', 30),
        ('政策场景(处理后)', 30),
        ('概念断言(处理前)', 50),
        ('概念断言(清洗后)', 50),
        ('概念约束文本', 40),
        ('时间约束(处理前)', 40),
        ('时间约束(处理后)', 40),
        ('最终提示词', 80),
        ('最终回答', 80)
    ]

    for col, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.border = thin_border
        cell.alignment = Alignment(wrap_text=True, vertical='center')
        ws.column_dimensions[cell.column_letter].width = width

    for row_idx, r in enumerate(results, 2):
        values = [
            r['question'],
            r.get('prompt', ''),
            r.get('articles_text', ''),
            json.dumps(r.get('raw_condition_pairs', []), ensure_ascii=False, indent=2),
            json.dumps(r.get('condition_pairs', []), ensure_ascii=False, indent=2),
            json.dumps(r.get('raw_scene_enum', []), ensure_ascii=False),
            json.dumps(r.get('scene_enum', []), ensure_ascii=False),
            json.dumps(r.get('raw_assertions', []), ensure_ascii=False, indent=2),
            json.dumps(r.get('assertions_cleaned', []), ensure_ascii=False, indent=2),
            '\n'.join(r.get('constraint_texts', [])),
            json.dumps(r.get('raw_time_constraints', []), ensure_ascii=False, indent=2),
            json.dumps(r.get('time_constraints', []), ensure_ascii=False, indent=2),
            r.get('final_prompt', ''),
            r.get('final_answer', '')
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            cell.border = thin_border

    wb.save(output_path)
