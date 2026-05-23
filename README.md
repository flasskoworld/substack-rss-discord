# Free RSS to Discord Webhook

This posts new articles from an RSS feed to a Discord channel through a Discord webhook.

It is already set up for:

```text
https://streeteconomics.substack.com/feed
```

## Important

Regenerate the Discord webhook you pasted into chat earlier. A Discord webhook URL is a secret; anyone who has it can post into that channel.

## Run Locally

First, mark the current posts as already seen so it does not post your whole archive:

```bash
DISCORD_WEBHOOK_URL="your-new-discord-webhook-url" python3 rss_to_discord.py --mark-existing
```

Then run it whenever you want to check for new posts:

```bash
DISCORD_WEBHOOK_URL="your-new-discord-webhook-url" python3 rss_to_discord.py
```

To post to multiple Discord webhooks, separate them with commas:

```bash
DISCORD_WEBHOOK_URLS="webhook-url-1,webhook-url-2" python3 rss_to_discord.py
```

To preview the Discord payload without posting:

```bash
python3 rss_to_discord.py --dry-run
```

If local macOS Python raises `CERTIFICATE_VERIFY_FAILED`, run Python's bundled
`Install Certificates.command`, or use this one-off local test workaround:

```bash
ALLOW_INSECURE_SSL=1 DISCORD_WEBHOOK_URL="your-new-discord-webhook-url" python3 rss_to_discord.py --max-posts 1
```

## Run Free on GitHub Actions

1. Create a new GitHub repo and add these files.
2. In the repo, go to `Settings -> Secrets and variables -> Actions`.
3. Add a repository secret named `DISCORD_WEBHOOK_URLS`.
4. Use your regenerated webhook as the value. For multiple webhooks, put one URL per line.
5. Go to the `Actions` tab and run `RSS to Discord` manually once with `mark_existing` checked.
6. After that, leave it alone. Future scheduled runs will post only new articles.

The workflow checks every 15 minutes and posts only new RSS items. It stores seen post IDs in `.rss_state.json`.

## Change the Feed

Edit `.github/workflows/rss-to-discord.yml` and change:

```yaml
RSS_FEED_URL: https://streeteconomics.substack.com/feed
```

For most Substacks, the feed is:

```text
https://YOURPUBLICATION.substack.com/feed
```
