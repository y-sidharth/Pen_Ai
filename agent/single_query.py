#!/usr/bin/env python3
"""
single_query.py — single-shot query with simple TF-IDF RAG persisted on the pendrive.
"""
import os
import sys
import json
import re
import shlex
import subprocess
import tempfile
import math
import time
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
PROMPTS_DIR = os.path.join(BASE_DIR, 'prompts')
SYSTEM_PROMPT_PATH = os.path.join(PROMPTS_DIR, 'system.txt')
DATA_DIR = os.path.join(BASE_DIR, 'data')
TMP_DIR = os.path.join(DATA_DIR, 'tmp')
BRAIN_DIR = os.path.join(BASE_DIR, 'brain')
INDEX_PATH = os.path.join(DATA_DIR, 'index.json')
os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs(BRAIN_DIR, exist_ok=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print('config.json not found', file=sys.stderr)
        sys.exit(2)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_system_prompt():
    if os.path.exists(SYSTEM_PROMPT_PATH):
        with open(SYSTEM_PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return 'You are Jampandu, a local offline assistant.'


def tokenize(text):
    return [t.lower() for t in ''.join([c if c.isalnum() else ' ' for c in text]).split() if len(t)>1]


def build_index():
    docs = []
    for root, _, files in os.walk(BRAIN_DIR):
        for fn in files:
            if fn.lower().endswith('.txt'):
                p = os.path.join(root, fn)
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        txt = f.read()
                    docs.append({'id': p, 'text': txt})
                except Exception:
                    pass
    if not docs:
        # no brain documents yet
        return {'docs': [], 'idf': {}, 'tf': {}}
    df = {}
    tf = {}
    for d in docs:
        toks = set(tokenize(d['text']))
        for t in toks:
            df[t] = df.get(t, 0) + 1
    N = len(docs)
    idf = {t: math.log((N+1)/(df[t]+1))+1 for t in df}
    for d in docs:
        terms = tokenize(d['text'])
        freqs = {}
        for t in terms:
            freqs[t] = freqs.get(t,0)+1
        # convert to tf-idf
        vec = {t: (freqs[t]/len(terms)) * idf.get(t,1.0) for t in freqs} if terms else {}
        norm = math.sqrt(sum(v*v for v in vec.values()))
        if norm>0:
            vec = {k: v/norm for k,v in vec.items()}
        tf[d['id']] = vec
    index = {'docs': [ {'id':d['id'], 'text': d['text'][:2000]} for d in docs ], 'idf': idf, 'tf': tf}
    try:
        with open(INDEX_PATH, 'w', encoding='utf-8') as f:
            json.dump(index, f)
    except Exception:
        pass
    return index


def load_index():
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return build_index()


def query_topk(index, q, k=3):
    if not index.get('docs'):
        return []
    qterms = tokenize(q)
    freqs = {}
    for t in qterms:
        freqs[t] = freqs.get(t,0)+1
    idf = index.get('idf',{})
    qvec = {t: (freqs[t]/len(qterms)) * idf.get(t,1.0) for t in freqs} if qterms else {}
    qnorm = math.sqrt(sum(v*v for v in qvec.values()))
    if qnorm>0:
        qvec = {k: v/qnorm for k,v in qvec.items()}
    scores = []
    for doc in index['docs']:
        did = doc['id']
        dvec = index.get('tf', {}).get(did, {})
        # dot product
        s = 0.0
        for t,v in qvec.items():
            s += v * dvec.get(t, 0.0)
        scores.append((s, doc))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [d for s,d in scores[:k] if s>0]


def _clean_llm_output(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</?think>', '', text, flags=re.IGNORECASE)
    m = re.search(r'\n\s*(User|Assistant)\s*:', text)
    if m:
        text = text[:m.start()].rstrip()
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def run_llama(llama_bin, model_path, prompt_file, cmd_template):
    cmd_str = cmd_template.format(bin=shlex.quote(llama_bin), model=shlex.quote(model_path), prompt_file=shlex.quote(prompt_file))
    cmd = shlex.split(cmd_str, posix=False) if os.name == 'nt' else shlex.split(cmd_str)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = proc.stdout.strip() or proc.stderr.strip()
        return _clean_llm_output(output)
    except Exception as e:
        return f'Error running inference binary: {e}'


def main():
    import argparse
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', '-t', help='Text to send to the assistant')
    source.add_argument('--input-file', help='UTF-8 text file to send to the assistant')
    parser.add_argument('--use_rag', action='store_true', help='Use local RAG')
    args = parser.parse_args()

    if args.input_file:
        try:
            with open(args.input_file, 'r', encoding='utf-8') as input_file:
                query_text = input_file.read()
        except (OSError, UnicodeError) as exc:
            print(f'Unable to read input file: {exc}', file=sys.stderr)
            sys.exit(2)
    else:
        query_text = args.text
    if not query_text.strip():
        print('Query text cannot be empty.', file=sys.stderr)
        sys.exit(2)

    cfg = load_config()
    system_prompt = read_system_prompt()
    # Ensure time is always injected so direct CLI calls also never hallucinate
    try:
        now = datetime.datetime.now().astimezone()
        tz = now.strftime("%Z") or "local"
        time_note = f"\nCurrent host local time: {now.strftime('%A, %B %d, %Y %I:%M %p')} {tz} (use this exact time for any time/date question)\n"
        if "Current host local time" not in system_prompt:
            system_prompt = system_prompt + "\n" + time_note
    except Exception:
        pass
    _llama_bin_raw = cfg.get('llama_bin') or ''
    _model_path_raw = cfg.get('model_path') or ''
    llama_bin = _llama_bin_raw if os.path.isabs(_llama_bin_raw) else os.path.join(BASE_DIR, _llama_bin_raw)
    model_path = _model_path_raw if os.path.isabs(_model_path_raw) else os.path.join(BASE_DIR, _model_path_raw)
    cmd_template = cfg.get('llama_cmd_template')

    # Load or build index from brain docs
    index = load_index()
    context = ''
    if args.use_rag:
        docs = query_topk(index, query_text, k=3)
        if docs:
            context = '\n---CONTEXT---\n' + '\n\n'.join(d['text'] for d in docs) + '\n---ENDCONTEXT---\n'

    prompt = system_prompt + '\n' + (context + '\n' if context else '') + f'User: {query_text}\nAssistant:'

    # Fast path: if a warm llama-server is already running (started by the web UI
    # or the CLI), reuse it instead of cold-loading the model for this popup.
    try:
        import llama_server
        if llama_server.is_healthy():
            _msgs = [{'role': 'system', 'content': system_prompt}]
            if context:
                _msgs.append({'role': 'system', 'content': context})
            _msgs.append({'role': 'user', 'content': query_text})
            warm_out = llama_server.chat(_msgs, timeout=120)
            if warm_out:
                print(_clean_llm_output(warm_out))
                return
    except Exception:
        pass

    prompt_file = os.path.join(TMP_DIR, f'popup_prompt_{int(time.time()*1000)}.txt')
    with open(prompt_file, 'w', encoding='utf-8') as tf:
        tf.write(prompt)

    if not os.path.exists(llama_bin) or not os.path.exists(model_path):
        print(f'(No local model or binary found. Place model at "{model_path}" and binary at "{llama_bin}" and update config.json.)')
        try:
            os.remove(prompt_file)
        except Exception:
            pass
        sys.exit(3)

    output = run_llama(llama_bin, model_path, prompt_file, cmd_template)

    try:
        os.remove(prompt_file)
    except Exception:
        pass

    print(output)

if __name__ == '__main__':
    main()
