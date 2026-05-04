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


# ===================== Skill System Prompts =====================

SKILL1_SYSTEM = """你是一个税务规则抽取专家。请从给定的文章中，抽取所有明确的"条件→结论"对。

输出格式：JSON数组，每个对象必须包含：
- condition: 触发条件（一句话，客观可判断）
- conclusion: 对应的结论/规则
- article_id: 输入的文章ID（原样输出）

要求：
1. 每条规则独立，不要合并多个条件。
2. 如果文章包含例外或限制条件，需单独拆分为带否定的条件。
3. 不要添加原文没有的信息。
4. 如果文章无明确规则，输出空数组[]。
5. 只输出JSON，不要其他解释。"""

SKILL2_SYSTEM = """你是一个税务场景分类专家。给定用户问题和相关文章，请提取所有可能适用的政策场景标签。

输出格式：JSON数组，每个元素是一个简短的场景标签（如"小微企业优惠"）。

要求：
1. 标签来源于文章中的政策名称或常见税务术语。
2. 标签应具有区分度，避免过于宽泛（如"税收优惠"不可取，应细化为"西部大开发优惠"）。
3. 不要遗漏文章中明确提到的场景。
4. 只输出JSON数组，不要其他解释。"""

SKILL3_SYSTEM = """你是一个税务概念关系抽取专家。请从给定的用户问题和文章中，抽取所有成对概念之间的逻辑关系。

关系类型严格限定为以下六种（必须从列表中选）：
- hypernym: A是B的一种（下位→上位）
- synonym: A和B完全等价
- related_not_equal: A和B相关但不可等同（警告模型不要划等号）
- mutually_exclusive: A和B不能同时适用
- succession: B替代A（可能带过渡期）
- property_of: A是B的一个属性/参数

要求：
1. 每条断言必须提供原文证据（evidence字段，摘录原文1-2句）。
2. 只抽取文章或问题中明确体现的关系，不要臆想。
3. 输出JSON数组，即使没有关系也输出空数组[]。
4. 不要输出其他解释。"""

SKILL4_SYSTEM = """你是一个税务时间信息抽取专家。从文章中提取所有与政策适用时间相关的约束。

输出格式：JSON数组，每个对象包含：
- policy_name: 政策名称或简称
- constraint_type: 枚举值 "valid_for"（适用条件）, "invalid_for"（不适用条件）, "transitional"（过渡期保留）
- condition: 具体的条件描述（如"2010年12月31日前新办企业"）
- article_id: 来源文章ID

要求：
1. 只抽取明确的时间限定，如"自X年X月X日起执行""停止执行""继续享受到期满"。
2. 如果政策有废止但保留存量，拆分为两条：一条invalid_for（对新办企业），一条transitional（对存量）。
3. 输出空数组若无时间信息。"""

SKILL3_CROSS_SYSTEM = """你是一个税务概念关系发现专家。现在已从多篇文章中分别抽取了概念关系断言，请你综合分析这些断言，发现跨文章之间的概念关系。

已有断言列表如下。请检查：
1. 不同文章中的概念是否实际上是同一个概念（应补充synonym关系）
2. 不同文章中的概念之间是否有上下位关系（hypernym）
3. 是否存在跨文章的互斥或替代关系（mutually_exclusive / succession）

用户问题：{question}

关系类型严格限定为以下六种：
- hypernym: A是B的一种（下位→上位）
- synonym: A和B完全等价
- related_not_equal: A和B相关但不可等同
- mutually_exclusive: A和B不能同时适用
- succession: B替代A（可能带过渡期）
- property_of: A是B的一个属性/参数

输出格式：JSON数组，每个对象包含：
- entity_a: 概念A名称
- entity_b: 概念B名称
- relation_type: 关系类型
- evidence: 推断依据（说明为什么认为这两个概念有此关系）

要求：
1. 只补充跨文章的新关系，不要重复已有断言。
2. 每条断言必须说明推断依据。
3. 输出JSON数组，若无新发现则输出空数组[]。
4. 不要输出其他解释。"""

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


def _merge_lists(list_of_lists):
    """合并多个JSON数组，去除完全重复的项（按JSON序列化去重）"""
    merged = []
    seen_keys = set()
    for lst in list_of_lists:
        if not lst or not isinstance(lst, list):
            continue
        for item in lst:
            if isinstance(item, dict):
                key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(item)
            elif isinstance(item, str):
                if item not in seen_keys:
                    seen_keys.add(item)
                    merged.append(item)
            else:
                merged.append(item)
    return merged


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
    constraints = "\n".join(constraint_texts) if constraint_texts else "无特殊概念约束"

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
            aid = cc.get('article_id', '')
            cc_lines.append(f"{i}. 条件：{cond}\n   结论：{conc}\n   来源：{aid}")
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

            # ---- Step 2: Run skills (Phase 1: per-article) ----
            # Skills 1/2/3/4: 全部逐篇调用
            tasks = []

            for art_idx, art_content in enumerate(article_parts):
                # 从文章内容中提取NTPSID作为article_id
                import re
                ntpsid_match = re.search(r'NTPSID:\s*(\d+)', art_content)
                art_id = f"ART_{ntpsid_match.group(1)}" if ntpsid_match else f"ART_{row_num}_P{art_idx + 1}"
                tasks.append((f'skill1_art{art_idx+1}', f'条件-结论对抽取(文章{art_idx+1})',
                              SKILL1_SYSTEM, f"文章ID: {art_id}\n文章内容:\n{art_content}", 'skill1'))
                tasks.append((f'skill2_art{art_idx+1}', f'政策场景枚举(文章{art_idx+1})',
                              SKILL2_SYSTEM, f"用户问题：{question}\n\n相关文章：\n{art_content}", 'skill2'))
                tasks.append((f'skill3_art{art_idx+1}', f'概念关系断言(文章{art_idx+1})',
                              SKILL3_SYSTEM, f"用户问题：{question}\n\n相关文章：\n{art_content}", 'skill3'))
                tasks.append((f'skill4_art{art_idx+1}', f'时间约束抽取(文章{art_idx+1})',
                              SKILL4_SYSTEM, f"用户问题：{question}\n\n相关文章：\n{art_content}", 'skill4'))

            _log(f"[stability] row={row_num} Phase1: {len(tasks)}个逐篇任务（{len(article_parts)}篇×4个Skill）")

            # Send start events
            for sid, slabel, ssys, suser, _group in tasks:
                yield _sse({'type': 'step_start', 'question_idx': q_idx,
                            'step_id': sid, 'step_label': slabel,
                            'system_prompt': ssys[:500], 'user_prompt': suser[:500]})

            # Run all Phase 1 tasks in parallel
            task_queue = queue.Queue()

            def _run_task(_sid, _slabel, _ssys, _suser, _group):
                try:
                    resp = _call_llm(_ssys, _suser, temperature=0.01, max_tokens=0, timeout=600)
                    parsed = _parse_json_response(resp)
                    if _group == 'skill3' and parsed and isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                item.setdefault('entity_a', item.pop('concept_A', ''))
                                item.setdefault('entity_b', item.pop('concept_B', ''))
                                item.setdefault('relation_type', item.pop('relation', ''))
                    if parsed is None and resp:
                        _log(f"[stability] row={row_num} {_slabel} JSON解析失败，原始响应前500字: {resp[:500]}")
                    task_queue.put((_sid, _slabel, resp, parsed, None, _group))
                except Exception as e:
                    task_queue.put((_sid, _slabel, None, None, str(e), _group))

            per_article_results = {'skill1': [], 'skill2': [], 'skill3': [], 'skill4': []}

            with ThreadPoolExecutor(max_workers=min(thread_count, len(tasks))) as executor:
                for sid, slabel, ssys, suser, group in tasks:
                    executor.submit(_run_task, sid, slabel, ssys, suser, group)

                completed = 0
                while completed < len(tasks):
                    sid, slabel, resp, parsed, error, group = task_queue.get()
                    completed += 1
                    if error:
                        _log(f"[stability] row={row_num} {slabel}失败: {error}")
                        yield _sse({'type': 'step_error', 'question_idx': q_idx,
                                    'step_id': sid, 'step_label': slabel, 'error': error})
                    else:
                        _log(f"[stability] row={row_num} {slabel}完成，原始响应:\n{resp}")
                        _log(f"[stability] row={row_num} {slabel}解析结果:\n{json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else 'None'}")
                        yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                                    'step_id': sid, 'step_label': slabel,
                                    'response': resp, 'parsed': parsed})
                        if group in per_article_results:
                            per_article_results[group].append(parsed if isinstance(parsed, list) else [])

            # ---- Step 2b: Skill 3 Phase 2 - 跨文章关系发现 ----
            phase1_assertions = _merge_lists(per_article_results['skill3'])

            if len(article_parts) > 1 and phase1_assertions:
                _log(f"[stability] row={row_num} Phase2: 跨文章关系发现（已有{len(phase1_assertions)}条断言）")
                yield _sse({'type': 'step_start', 'question_idx': q_idx,
                            'step_id': 'skill3_cross', 'step_label': '跨文章概念关系发现',
                            'system_prompt': SKILL3_CROSS_SYSTEM[:500],
                            'user_prompt': f'已有断言: {json.dumps(phase1_assertions, ensure_ascii=False)[:500]}'})

                try:
                    cross_prompt = (
                        f"用户问题：{question}\n\n"
                        f"已有断言列表：\n{json.dumps(phase1_assertions, ensure_ascii=False, indent=2)}"
                    )
                    cross_resp = _call_llm(SKILL3_CROSS_SYSTEM, cross_prompt,
                                          temperature=0.01, max_tokens=0, timeout=600)
                    cross_parsed = _parse_json_response(cross_resp)
                    if cross_parsed and isinstance(cross_parsed, list):
                        for item in cross_parsed:
                            if isinstance(item, dict):
                                item.setdefault('entity_a', item.pop('concept_A', ''))
                                item.setdefault('entity_b', item.pop('concept_B', ''))
                                item.setdefault('relation_type', item.pop('relation', ''))
                        _log(f"[stability] row={row_num} 跨文章关系发现完成，新增{len(cross_parsed)}条")
                    else:
                        cross_parsed = []
                        _log(f"[stability] row={row_num} 跨文章关系发现: 无新增断言")

                    yield _sse({'type': 'step_complete', 'question_idx': q_idx,
                                'step_id': 'skill3_cross', 'step_label': '跨文章概念关系发现',
                                'response': cross_resp, 'parsed': cross_parsed})
                    phase1_assertions.extend(cross_parsed)
                except Exception as e:
                    _log(f"[stability] row={row_num} 跨文章关系发现失败: {e}")
                    yield _sse({'type': 'step_error', 'question_idx': q_idx,
                                'step_id': 'skill3_cross', 'step_label': '跨文章概念关系发现',
                                'error': str(e)})

            # Merge all results
            products['condition_pairs'] = _merge_lists(per_article_results['skill1'])
            products['scene_enum'] = _dedupe_strings(_merge_lists(per_article_results['skill2']))
            products['assertions_raw'] = phase1_assertions
            products['time_constraints'] = _merge_lists(per_article_results['skill4'])

            _log(f"[stability] row={row_num} 逐篇合并结果: 条件-结论={len(products['condition_pairs'])}条, "
                 f"场景={len(products['scene_enum'])}个, 断言={len(products['assertions_raw'])}条, "
                 f"时间约束={len(products['time_constraints'])}条")

            # ---- Step 3: Validate assertions ----
            raw_count = len(products['assertions_raw'])
            cleaned = _validate_assertions(products['assertions_raw'])
            products['assertions_cleaned'] = cleaned
            constraint_texts = _convert_constraints_to_text(cleaned)
            products['constraint_texts'] = constraint_texts

            _log(f"[stability] row={row_num} 断言校验: {raw_count}→{len(cleaned)}")
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
                'condition_pairs': products['condition_pairs'],
                'scene_enum': products['scene_enum'],
                'assertions_cleaned': products['assertions_cleaned'],
                'constraint_texts': products['constraint_texts'],
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
    """Save results to Excel"""
    wb = Workbook()
    ws = wb.active
    ws.title = '回答稳定性结果'

    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    headers = [
        ('问题', 50), ('提示词(B列)', 40), ('文章内容', 80),
        ('条件-结论对', 60), ('政策场景', 30),
        ('概念断言(清洗后)', 50), ('概念约束文本', 40),
        ('时间约束', 40), ('最终提示词', 80), ('最终回答', 80)
    ]

    for col, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.border = thin_border
        cell.alignment = Alignment(wrap_text=True, vertical='center')
        ws.column_dimensions[cell.column_letter].width = width

    for row_idx, r in enumerate(results, 2):
        values = [
            r['question'], r.get('prompt', ''), r.get('articles_text', ''),
            json.dumps(r.get('condition_pairs', []), ensure_ascii=False, indent=2),
            json.dumps(r.get('scene_enum', []), ensure_ascii=False),
            json.dumps(r.get('assertions_cleaned', []), ensure_ascii=False, indent=2),
            '\n'.join(r.get('constraint_texts', [])),
            json.dumps(r.get('time_constraints', []), ensure_ascii=False, indent=2),
            r.get('final_prompt', ''), r.get('final_answer', '')
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            cell.border = thin_border

    wb.save(output_path)
