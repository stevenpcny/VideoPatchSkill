#!/usr/bin/env python3
"""Estimate how many tokens an agent spends reading flow-video-patch documents, per task scenario.

  python tools/token_budget.py                 # table for the checked-out package
  python tools/token_budget.py --json out.json # also write the numbers

Read-only, standard library only. The estimate is a heuristic, not a tokenizer: one token per
CJK character (incl. full-width punctuation) plus one token per 3.2 other characters. It is meant
for comparing read paths before and after a documentation change in the same repository, not for
billing. Check absolute numbers against your host's usage logs before quoting them.

Scenarios follow the reading rules in SKILL.md / AGENT_ENTRY.md as written; a scenario lists the
files those rules send an agent to, whole-file, because the current documents have no section
level routing. Edit SCENARIOS when the reading rules change.
"""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT/'flow-video-patch'
CJK = re.compile(r'[　-〿一-鿿＀-￯]')

ALWAYS = ['AGENT_ENTRY.md', 'SKILL.md']
FIRST_USE = ['references/tool-policy.md', 'TOOL_POLICY.json', 'SECURITY.md']
SCENARIOS = {
    # First task on a machine: one person, one Flow return, face patch, normal deliverable.
    'first_task_single_return': ALWAYS + FIRST_USE + [
        'references/workflow.md', 'references/parameters.md', 'references/prompts.md',
        'references/patch-lessons.md', 'references/field-lessons.md',
        'references/return-fit-assessment.md', 'references/verification.md',
        'assets/visual-review-template.json'],
    # Same machine, later task, same package and dependency versions.
    'repeat_task_single_return': ALWAYS + [
        'references/parameters.md', 'references/prompts.md', 'references/field-lessons.md',
        'references/return-fit-assessment.md', 'references/verification.md',
        'assets/visual-review-template.json'],
    # Two people / overlap, colour on exposed skin, editable Resolve handoff requested.
    'complex_multi_person': ALWAYS + FIRST_USE + [
        'references/workflow.md', 'references/parameters.md', 'references/prompts.md',
        'references/patch-lessons.md', 'references/cases.md', 'references/field-lessons.md',
        'references/hd-relay-lessons.md', 'references/return-fit-assessment.md',
        'references/overlap-and-local-donors.md', 'references/skin-match.md',
        'references/advanced-compositing.md', 'references/verification.md',
        'references/resolve-project-handoff.md', 'assets/visual-review-template.json',
        'assets/resolve-handoff-template.json'],
}


def estimate(text):
    cjk = len(CJK.findall(text))
    return round(cjk + (len(text) - cjk)/3.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--json', type=Path, help='also write the numbers to this file')
    args = ap.parse_args()
    files = sorted({f for s in SCENARIOS.values() for f in s} |
                   {str(p.relative_to(PACKAGE)).replace('\\', '/') for p in PACKAGE.glob('references/*.md')} |
                   {'VALIDATION.json', 'CHANGELOG.md'})
    per_file = {f: estimate((PACKAGE/f).read_text(encoding='utf-8')) for f in files}
    scenarios = {name: {'files': len(fs), 'estimated_tokens': sum(per_file[f] for f in fs)}
                 for name, fs in SCENARIOS.items()}
    for f in files:
        print(f'{per_file[f]:7d}  {f}')
    print()
    for name, s in scenarios.items():
        print(f'{s["estimated_tokens"]:7d}  scenario {name} ({s["files"]} files)')
    if args.json:
        args.json.write_text(json.dumps({
            'method': 'heuristic: 1 token per CJK char + 1 per 3.2 other chars; whole-file reads',
            'per_file_estimated_tokens': per_file, 'scenarios': scenarios},
            ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
