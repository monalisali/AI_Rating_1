#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
多 search_tool 自动打分脚本
按 enabled_search_tools=[1]~[6] 依次执行模型打分，每次切换前修改 config.json
"""

import json
import os
import sys
import time

import requests

# ===== 配置 =====
BASE_URL = os.environ.get('FLASK_BASE_URL', 'http://127.0.0.1:5002')
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT_DIR, 'config.json')
"""
编号	工具
1	search_by_index_files
2	search_es_by_keyword
3	advanced_es_search
4	search_milvus_vector
5	web_search
6	get_related_articles

"""
# TOOL_IDS = [1, 2, 3, 4, 5, 6]
TOOL_IDS = [5,6]
ROUNDS = 1
THREAD_COUNT = 1

TOOL_NAMES = {
    1: 'search_by_index_files',
    2: 'search_es_by_keyword',
    3: 'advanced_es_search',
    4: 'search_milvus_vector',
    5: 'web_search',
    6: 'get_related_articles',
}
# ================


def ts():
    return time.strftime('%H:%M:%S')


def update_config(tool_id):
    """修改 config.json 中的 enabled_search_tools"""
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['enabled_search_tools'] = [tool_id]
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    tool_name = TOOL_NAMES.get(tool_id, '?')
    print(f"[{ts()}] [config] enabled_search_tools 已设为 [{tool_id}]({tool_name})")


def upload_file(filepath):
    """上传Excel文件，返回文件名"""
    filename = os.path.basename(filepath)
    with open(filepath, 'rb') as f:
        resp = requests.post(f'{BASE_URL}/model_scoring_upload', files={'file': (filename, f)})
    resp.raise_for_status()
    data = resp.json()
    if 'error' in data:
        print(f"[上传失败] {data['error']}")
        sys.exit(1)
    print(f"[{ts()}] [上传成功] 文件: {data['filename']}, 题数: {data['question_count']}")
    return data['filename']


def run_scoring(filename, tool_id):
    """调用模型打分接口，等待SSE流结束"""
    print(f"\n{'='*60}")
    print(f"[{ts()}] 开始处理 enabled_search_tools=[{tool_id}]({TOOL_NAMES.get(tool_id, '?')})")
    print(f"{'='*60}")

    resp = requests.post(
        f'{BASE_URL}/model_scoring_process',
        json={
            'filename': filename,
            'rounds': ROUNDS,
            'thread_count': THREAD_COUNT
        },
        stream=True
    )
    resp.raise_for_status()

    output_file = None
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith('data: '):
            continue
        payload = json.loads(line[6:])
        if payload.get('type') == 'progress':
            r = payload['result']
            status = "成功" if r.get('success') else "失败"
            print(f"  [{payload['current']}/{payload['total']}] 行{r['row']} - {status}")
        elif payload.get('type') == 'complete':
            output_file = payload.get('output_filename')
            print(f"[完成] 结果文件: {output_file}")

    return output_file


def rename_output(output_rel_path, tool_id):
    """将接口生成的默认文件名重命名为包含查询条件和搜索方法名的格式"""
    if not output_rel_path:
        return None
    old_path = os.path.join(ROOT_DIR, 'outputs', output_rel_path)
    if not os.path.exists(old_path):
        print(f"[警告] 文件不存在: {old_path}")
        return output_rel_path

    tool_name = TOOL_NAMES.get(tool_id, f'unknown_tool_{tool_id}')
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    new_filename = f"模型打分结果_查询条件{tool_id}_{tool_name}_{timestamp}.xlsx"
    new_path = os.path.join(ROOT_DIR, 'outputs', '模型打分结果', new_filename)

    os.rename(old_path, new_path)
    new_rel = f"模型打分结果/{new_filename}"
    print(f"[{ts()}] [重命名] {output_rel_path} -> {new_rel}")
    return new_rel


def main():
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <Excel文件路径>")
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"文件不存在: {filepath}")
        sys.exit(1)

    # 1. 上传文件
    filename = upload_file(filepath)

    # 2. 依次按 tool_id 执行
    results = {}
    for tool_id in TOOL_IDS:
        update_config(tool_id)
        output = run_scoring(filename, tool_id)
        results[tool_id] = rename_output(output, tool_id)
        time.sleep(2)  # 间隔2秒，避免服务端压力

    # 3. 汇总
    print(f"\n{'='*60}")
    print(f"[{ts()}] 全部完成，各轮结果文件:")
    for tid, out in results.items():
        tool_name = TOOL_NAMES.get(tid, '?')
        print(f"  查询条件{tid}({tool_name}): {out}")


if __name__ == '__main__':
    main()
