"""Run the approved low-effort pilot, then 18 formal runs in separate directories."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys


def main():
    here = Path(__file__).resolve().parent
    output = here / 'results' / ('memory-low-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True, exist_ok=False)
    state = {'status': 'pilot_running', 'reasoning_effort': 'low',
             'formal_runs_requested': 18, 'pilot': str(output / 'pilot'),
             'formal': str(output / 'formal')}

    def checkpoint(status):
        state['status'] = status
        (output / 'campaign.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        print(f'CAMPAIGN {status}: {output}', flush=True)

    def run(stage):
        command = [sys.executable, str(here / 'benchmark_memory.py'), '--output', str(output / stage)]
        if stage == 'formal':
            command.append('--formal')
        code = subprocess.call(command, cwd=here.parent)
        if (output / stage / 'results.json').exists():
            subprocess.run([sys.executable, str(here / 'summarize_memory.py'), str(output / stage)],
                           cwd=here.parent, check=True)
        return code

    checkpoint('pilot_running')
    if run('pilot'):
        checkpoint('pilot_infrastructure_error')
        return 1
    gate = json.loads((output / 'pilot' / 'pilot-gate.json').read_text(encoding='utf-8'))
    state['pilot_gate'] = gate
    # Task pass/fail is an outcome, not a reason to hide a run. Readiness requires
    # working protocol/usage, actual LLM compression and a within-window control.
    readiness = ['both_completed', 'llm_compaction_observed', 'control_below_window', 'exact_usage']
    if not all(gate[k] for k in readiness):
        checkpoint('pilot_design_gate_failed')
        return 1
    checkpoint('formal_running')
    if run('formal'):
        checkpoint('formal_infrastructure_error')
        return 1
    checkpoint('complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
