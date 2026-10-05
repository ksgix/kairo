# Kairo

Kairo is a **persistent autonomous runtime**: a long-lived environment on a Linux
host in which cognition operates. The cognition itself (Claude, OpenAI, Gemini, …)
is external. Kairo is the runtime, not the model.

Kairo is **not** a chatbot, an agent manager, a multi-agent framework, an
orchestrator, a task manager or a project manager.

## Documentation

- [Architecture](docs/architecture.md): how the runtime, cognition, work and recovery fit together
- [Self-maintenance](docs/self-maintenance.md): releases, deployment and installing on a host
- [Operator interface](docs/operator.md): talking to a running Kairo
- [Dashboard](docs/dashboard.md): the browser interface
- [All docs](docs/README.md)

## Running (development)

```sh
cd /opt/kairo
export PYTHONPATH=src

# run continuously in the foreground (Ctrl-C also stops it cleanly)
python3 -m kairo --run --db var/kairo.db [--socket var/kairo.sock] [--reassess SECONDS] \
    [--cognition claude [--model MODEL] [--cognition-timeout SECONDS]]

# from another terminal, the operator client (see [Operator interface](docs/operator.md))
python3 -m kairo.ipc status
python3 -m kairo.ipc message "hello Kairo"   # stored in chat; wakes Kairo
python3 -m kairo.ipc chat                    # the conversation, with Kairo's replies
python3 -m kairo.ipc stop                    # graceful shutdown

# single cycle: start, cycle, print status as JSON, stop
python3 -m kairo --db var/kairo.db
```

- The socket defaults to `$KAIRO_SOCKET`, or `var/kairo.sock` relative to the current directory. Run the client from the same directory or pass `--socket`.
- The socket is created with mode `0600` and removed on shutdown. A stale socket left by a crashed process is replaced; a socket another live process is listening on is never touched.
- The IPC protocol is one JSON object per line in each direction, one request per connection; see [Operator interface](docs/operator.md).
- `--reassess` defaults to 300 seconds; `0` means sleep until woken. Memory defaults to `$KAIRO_DB` or `~/.local/share/kairo/kairo.db`.
- With `pip install -e .`, `kairo` replaces `python3 -m kairo`.

## Dashboard

A browser interface for the operator, listening on loopback only. It holds no Kairo state and
translates each route into one operator IPC operation. Details in [docs/dashboard.md](docs/dashboard.md).

```sh
PYTHONPATH=src python3 -m kairo.dashboard --socket var/kairo.sock      # http://localhost:8765/
```

## Tests

```sh
cd /opt/kairo
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests need no network, credentials or third-party packages.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
