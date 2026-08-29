# WebMCP-Bench

WebMCP-Bench models a website flow as a finite-state machine (FSM), where states describe what is currently true and transitions describe valid tool actions.

Each evaluation task expresses a natural-language user goal tied to one expected transition. Every task runs in a fresh sandbox with isolated fixtures, website execution, authoritative-state verification, metrics, and cleanup. The evaluation dashboard shows exactly what tasks passed or what failed.

## Usage

Requires Python 3.11+ and Daytona API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo "DAYTONA_API_KEY=your-key" > .env
python app.py examples/reading-list
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), then click **Run evaluation**.

Example website is at [http://127.0.0.1:8000/demo/](http://127.0.0.1:8000/demo/).

## Flow

For each task, WebMCP-Bench:

1. Creates an isolated sandbox.
2. Uploads the exact project, fixture, and task.
3. Starts and smoke-tests the website.
4. Runs the deterministic actor through the project adapter and downloads authoritative state.
5. Records commands, resource metrics, lifecycle, and cleanup.

The dashboard shows passes, semantic failures, infrastructure errors, and the evidence behind every result.

