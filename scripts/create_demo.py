"""Create a new synthetic workspace. Never reads existing user projects/chats."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Store

def create(target):
    root = Path(target).expanduser().resolve()
    if root.exists():
        raise SystemExit('Refusing an existing directory. Choose a new demo directory.')
    root.mkdir(parents=True)
    store = Store(root)
    store.init('Release readiness demo', 'Ship a small release with traceable evidence and explicit review')
    counter = 0
    def call(name, **args):
        nonlocal counter
        counter += 1
        return store.call(name, {**args, 'request_id': f'demo-{counter}'})
    call('task_create', id='TASK-001', title='Check the quick-start instructions', body='Start from an empty directory, initialize state and verify CLI/web project identity.')
    call('task_create', id='TASK-002', title='Review source and evidence', body='Verify the synthetic evidence file and document any uncertainty.', depends_on=['TASK-001'])
    call('task_create', id='TASK-003', title='Approve the release scope', body='Human review is separate from test completion.', depends_on=['TASK-002'])
    call('human_gate_request', id='REVIEW-001', title='Human release review', task_id='TASK-003', body='No automated approval. Check the exact release and decide explicitly.')
    evidence = root/'example-evidence.txt'
    evidence.write_text('Synthetic evidence only. No production or user acceptance claim.\n', encoding='utf-8')
    call('evidence_add', id='EVIDENCE-001', title='Synthetic installation note', body='A locator for demonstrating evidence reading; not a real verification result.', source={'uri': str(evidence), 'locator': 'line 1'})
    call('idea_capture', id='IDEA-001', title='Improve the default full-graph reading scale', body='A proposal, not an approved change to graph behavior.')
    print(root)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 scripts/create_demo.py /absolute/new/demo-directory')
    create(sys.argv[1])
