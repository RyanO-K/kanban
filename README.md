# Kanban Board

A lightweight, file-based kanban system for managing development tickets. Perfect for small teams and solo developers who want a simple, self-contained board without external dependencies.

## Quick Start

### Prerequisites
- Python 3.7+

### Installation

1. Clone or navigate to the `.kanban` directory.

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Running the Server

Start the kanban server and web UI:

```bash
python app/kanban_server.py
```

The UI will be available at `http://localhost:8745`

### Optional: Run the Orchestrator

For autonomous agent-based ticket dispatch (requires the Claude Code environment):

```bash
python app/orchestrator.py
```

The orchestrator automatically picks up tickets marked `ready` and dispatches headless agents to work them.

## Features

- **File-based tickets**: Each ticket is a JSON file — no database required
- **Web UI**: Simple HTML/CSS board with drag-and-drop columns
- **REST API**: Optional programmatic access via the built-in server
- **Worktree-based workflows**: Integrated git worktree support for isolated feature branches
- **Agent dispatch**: Autonomous orchestrator for Claude Code agents
- **Performance monitoring**: Built-in process monitor for tracking CPU/memory usage

## Board Structure

```
.kanban/
├── app/                   # The application
│   ├── kanban_server.py   #   Web server and API
│   ├── orchestrator.py    #   Autonomous agent dispatcher (runtime)
│   ├── orchestrator_core.py  # Dispatcher decision logic (unit-tested)
│   └── perf_monitor.py    #   Process/CPU monitor for the Performance tab
├── scripts/               # One-shot maintenance scripts
├── static/                # Web UI assets (kanban.html/css/js, served at /)
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── boards/<board-name>/
│   ├── _meta.json         # Board settings and context
│   └── *.json             # Ticket files (numbered: 1.json, 2.json, ...)
├── config/                # Agent profiles and dispatcher configuration
├── skills/                # Reusable board-wide skills for agents
└── tests/                 # Test suite
```

## Ticket Workflow

1. **Create** a ticket by adding a new JSON file to a board directory
2. **Organize** into columns: `todo`, `ready`, `in_progress`, `pending`, `completed`
3. **Move** tickets by changing the `status` field
4. **Track progress** with comments and history entries

See `CLAUDE.md` for the complete ticket schema and agent interaction guide.

## Using with Claude Code

If you're using this kanban with Claude Code agents:

1. Start the orchestrator: `python app/orchestrator.py`
2. Create a board in the UI (or add a directory with `_meta.json`)
3. Enable the orchestrator in the UI's **Orchestrator** tab
4. Agents will automatically pick up `ready` tickets and move them to `in_progress`

## Configuration

### Server Bind Address

To change the server's host and port, create `.kanban/_orchestrator/server.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8745
}
```

Defaults:
- **host**: Loopback (`127.0.0.1`) — set to `0.0.0.0` for network access
- **port**: `8745`

### Orchestrator Settings

Configure in the UI's **Orchestrator** tab or edit `.kanban/_orchestrator/state.json`:

```json
{
  "enabled": true,
  "concurrencyCap": 3,
  "stopAllRequested": false
}
```

## API Endpoints

All endpoints return JSON and are available when the server is running.

| Action | Endpoint |
|--------|----------|
| List boards | `GET /api/files` |
| Load board | `GET /api/board/<slug>` |
| Create ticket | `POST /api/board/<slug>/task` |
| Move ticket | `PATCH /api/board/<slug>/task/<id>` |
| Add comment | `POST /api/board/<slug>/task/<id>/comment` |
| Delete ticket | `DELETE /api/board/<slug>/task/<id>` |

Example:
```bash
curl http://localhost:8745/api/board/kanban-dev
```

## Testing

Run the test suite:

```bash
python -m pytest tests
python -m pytest tests -v
python -m pytest tests/test_orchestrator_core.py
```

## Troubleshooting

**Port already in use?**
```bash
python app/kanban_server.py 9000  # Use a different port
```

**Performance tab shows "Install psutil"?**
```bash
pip install psutil  # Optional dependency for better monitoring
```

**Tickets not dispatching?**
- Check that the orchestrator is running
- Verify `state.json` has `enabled: true`
- Ensure tickets have `status: "ready"`
- Check the orchestrator log in `.kanban/_orchestrator/runs/`

## Documentation

- `CLAUDE.md` — Complete guide to the board structure, agent conventions, and git workflows
- `orchestrator_triage_prompt.md` — How the orchestrator prioritizes and selects tickets

## Contributing

Edit the JSON files directly to create/update tickets, or use the web UI for drag-and-drop operations. All changes are logged in each ticket's `history` field for full auditability.
