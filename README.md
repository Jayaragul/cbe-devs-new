# AI Signal

AI Signal is a CBE Devs project covering model releases, AI research, infrastructure, and policy. A sweep opens the matching publisher pages, checks publication time and relevance, and records why candidates were excluded. The default briefing covers seven days.

## Run locally

Requires Python 3.11 or newer. The local server and tests use the standard library.

~~~bash
python ai_signal/live/local_server.py 8787
~~~

Open [AI Signal](http://127.0.0.1:8787), then select **Run sweep**. The briefing searches 12 public feeds. Use the filters and search box to find stories within the report. Set the reporting window before running a sweep.

The **Watchlist** lets you edit companies, aliases, or Boolean topic queries. For example:

~~~text
("reasoning model" OR "multimodal model") AND ("open weights" OR "open-weight")
~~~

Live reports appear in **Archive**. Each report includes a verification record and the latest feed status. Company announcements carry a source label, and each live story opens its original page.

## Publish to GitHub Pages

[Follow the GitHub Pages deployment guide](deploy/README.md#github-pages). The included GitHub Actions workflow generates and publishes an updated briefing three times a day. To change the published watchlist, edit `watchlist.json` and push the change to the default branch.

## Offline demo

Set `AI_SIGNAL_DEMO_MODE=1` before starting the local server, then run a sweep from the dashboard. The sample articles are fictional and excluded from the shared archive.

## Storage and scheduling

AI Signal seeds a new watchlist from `watchlist.json`. Local storage defaults to `data/ai-signal`. Use `AI_SIGNAL_DATA_DIR` to select a different directory. An existing watchlist at the configured location takes precedence over the seed.

Run an unattended live sweep with `python sweep_runner.py`. Use [the environment example](.env.example) to configure storage and optional search providers. See the [Cloud Run setup](deploy/README.md) and [Windows build instructions](desktop/README.md) for distribution.

## Tests

~~~bash
python -m unittest discover -s tests
python -m unittest discover -s desktop
~~~
