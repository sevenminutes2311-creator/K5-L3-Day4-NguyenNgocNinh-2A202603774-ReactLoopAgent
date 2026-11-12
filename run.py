import argparse
import json
import os
import re
import sys
from agent import run_agent
from providers import Provider
from status_tools import StatusTools
from bonus.tools import BonusTools


def load_env_file(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(path):
        return

    with open(path, encoding='utf-8-sig') as env_file:
        for line_number, raw_line in enumerate(env_file, 1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('$env:'):
                line = line[len('$env:'):].strip()
            elif line.startswith('export '):
                line = line[len('export '):].strip()
            if '=' not in line:
                raise ValueError(f'.env dòng {line_number} không phải phép gán KEY=value.')

            key, value = (part.strip() for part in line.split('=', 1))
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError(f'.env dòng {line_number} có tên biến không hợp lệ.')
            if value.startswith(("'", '"')):
                quote = value[0]
                if len(value) < 2 or value[-1] != quote:
                    raise ValueError(f'.env dòng {line_number} có giá trị chưa đóng dấu nháy.')
                value = value[1:-1]
            os.environ.setdefault(key, value)


def run(question, mode, provider_name, model, max_steps, fail=False, bonus=False):
    provider = Provider(provider_name, model)
    tools = BonusTools(mode) if bonus else StatusTools(mode, fail=fail)
    return run_agent(question, provider, tools, max_steps=max_steps)


def main():
    try:
        load_env_file()
    except (OSError, ValueError) as exc:
        print(f'Lỗi cấu hình .env: {exc}', file=sys.stderr)
        return 1

    p = argparse.ArgumentParser(description='Chạy status agent')
    p.add_argument('question', help='Câu hỏi cho agent')
    p.add_argument('--bonus', action='store_true', help='Dùng bộ tools bonus')
    p.add_argument('--mode', choices=['snapshot', 'live'], default='snapshot')
    p.add_argument('--provider', choices=['openai', 'gemini', 'anthropic', 'openrouter'], default=os.getenv('PROVIDER', 'openai'))
    p.add_argument('--model', default=os.getenv('MODEL'))
    p.add_argument('--max-steps', type=int, default=6)
    args = p.parse_args()
    if args.max_steps < 1:
        p.error('--max-steps phải >= 1')
    try:
        result = run(args.question, args.mode, args.provider, args.model, args.max_steps, bonus=args.bonus)
    except (ValueError, RuntimeError, NotImplementedError) as exc:
        print(f'Lỗi: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'completed' else 1

if __name__ == '__main__':
    sys.exit(main())