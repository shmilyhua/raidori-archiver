[![AI Generated](https://img.shields.io/badge/AI_Generated-Gemini-blue.svg)](https://gemini.google.com)
## Acknowledgments

* **Code Generation:** The core logic and boilerplate for this project were generated using Gemini. All AI-generated code was subsequently reviewed and tested.
# raidori-archiver
An automated archiving tool for Raidori fanclubs. Downloads articles, images, audio, comments, and replies, formatting them into an offline-viewable HTML interface.

## Features
- **Offline HTML Rendering:** Rebuilds TipTap rich text, embedded media, and comment threads locally.
- **Media Archival:** Automatically downloads cover images, inline images, and attached audio files.
- **Pagination & Rate Limiting:** Safely handles large accounts via GraphQL pagination and strict request throttling to prevent IP bans.
- **Price Filtering:** Selectively skip premium posts above a designated JPY threshold.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shmilyhua/raidori-archiver.git
   cd raidori-archiver
   ```

2. This project uses `uv` for dependency management. Install the dependencies:
   ```bash
   uv venv --python 3.13
   uv sync
   ```
   *(Alternatively, you can install the dependencies listed in `pyproject.toml` via pip).*

## Configuration

1. Create a `config.json` file in the root directory. This file is ignored by git to protect your credentials.
2. Structure your `config.json` as follows:

```json
{
    "cookies": "__session_v2=YOUR_RAIDORI_COOKIE_STRING_HERE",
    "target_users": ["@username"],
    "max_price": 10000,
    "crawl_interval": 10.0,
    "output_dir": "./raidori_archive",
    "request_timeout": 40.0
}
```
cookies: Your active session cookie string from your browser network tab.

target_users: A list of handles (with or without @) or numeric User IDs to archive.

max_price: The maximum price in JPY to download. Set to 0 for free-only, or null for no limit.

crawl_interval: Seconds to pause between every network request to prevent rate limiting.

## Usage

Start the archiver by running:

```bash
uv run raidori_archiver.py
```

The script will create a directory named raidori_archive containing separate folders for each creator, populated with the downloaded assets and the article.html viewer.
