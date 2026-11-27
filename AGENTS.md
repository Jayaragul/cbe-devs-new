# AI Signal agent instructions

AI Signal is CBE Devs' AI news briefing. Keep the local Python app and the static GitHub Pages build working from the same verification pipeline.

## News verification

- Use `watchlist.json` for published coverage. It defines the search window and the companies and topics to follow. The local app can also read an edited watchlist from its configured storage.
- Use public feeds for discovery. Follow each candidate to its publisher and require publication evidence from that page. Search snippets, feed timestamps, and modified timestamps do not establish when a story was published.
- Check that the story falls in the reporting window and describes a concrete development. For company stories, confirm the company and the AI connection in the headline or publisher summary. Preserve source labels and links to the publisher.
- Keep offline demo reports separate from live reports. Preserve feed health and rejection reasons so readers can distinguish an empty briefing from a failed search. The Pages build should stop if no feed returns entries.

## Development and verification

- Use Python 3.11 or newer. The local app, tests, and Pages builder use the standard library.
- Run the app with `python ai_signal/live/local_server.py 8787`. Set `AI_SIGNAL_DEMO_MODE=1` before launch for an offline preview.
- Run `python -m unittest discover -s tests` and `python -m unittest discover -s desktop` before publishing changes.
- Build a static preview with `python tools/build_pages.py --output _site/cbe-devs-new`, then serve `_site` with `python -m http.server 8000 --bind 127.0.0.1 --directory _site`. Open `http://127.0.0.1:8000/cbe-devs-new/` to test the same subpath layout as GitHub Pages.
- Keep asset and snapshot URLs relative in the static site. The Pages watchlist is read-only and loads from `data/briefing.json`. Watchlist writes and manual sweeps belong to the Python server.

## GitHub Pages deployment

- Follow [the deployment guide](deploy/README.md#github-pages). The publishing workflow is `.github/workflows/publish-pages.yml`. It runs on pushes to `main`, on schedule, and on demand.
- Before the first deployment, select **GitHub Actions** under **Settings → Pages → Source**. If **Configure Pages** fails because Pages is disabled, check that setting before retrying the workflow.
- For deployment status, inspect the **Publish AI Signal** run for the relevant commit. If it failed, identify the failed job and step. If it passed, open the URL from the deploy job and confirm that the briefing renders with its reporting date or story count.
- Report push status and deployment status separately. A clean push confirms that GitHub has the commit. The deploy job and the live URL are the evidence that Pages is serving it.
