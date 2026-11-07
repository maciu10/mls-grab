# mls-grab

Downloads every photo from an MLS or real-estate listing page. It needs only Python 3.9+ and no other packages.

```bash
pip install .            # installs the `mls-grab` command (or use: python -m mls_grab)

# By listing page URL
mls-grab https://your-mls-site.example/listing/1683368

# By bare listing ID, with a URL template for your MLS site
mls-grab 1683368 --url-template 'https://your-mls-site.example/listing/{id}'
export MLS_GRAB_URL_TEMPLATE='https://your-mls-site.example/listing/{id}'   # set once
mls-grab 1683368

# Only list the photo URLs
mls-grab URL --list            # or --list --json
```

Photos go to `listing-<id>/` as `001.jpg`, `002.jpg`, … in gallery order. A `sources.json` file maps each file back to its original URL.

## How it finds photos

It looks in every place listing sites commonly put gallery images:

- schema.org **JSON-LD** (`image`, `photo`, `ImageObject.contentUrl`)
- `og:image` / `twitter:image` meta tags
- `<img>` / `<source>`: `src`, `srcset`, and lazy-load attributes (`data-src`, `data-lazy`, `data-original`, …)
- `<a href="…jpg">` links to full-size images
- image URLs inside embedded app state such as `__NEXT_DATA__` or `window.__INITIAL_STATE__`, including JSON-escaped `\/` URLs
- CSS `background-image: url(…)`

After that it:

- drops logos, icons, sprites, agent headshots, tracking pixels, SVG and `data:` URIs
- merges size variants of the same photo (`/320x240/a.jpg` and `/1024x768/a.jpg`, `a_thumb.jpg`, `?w=400` and `?w=1600`) and keeps the largest
- checks each download's magic bytes and skips non-images, files under `--min-bytes` (default 20 KB), images under `--min-dim` px (default 300), and byte-identical duplicates

## When a site loads its gallery with JavaScript or needs a login

Open the listing in your browser, let the gallery load, and save the page with **Save Page As → HTML only**. Then run:

```bash
mls-grab --html saved.html --base-url 'https://site/listing/1683368'
```

You can also pass cookies or other headers: `-H 'Cookie: session=…'` (repeatable).

## Options

| flag | meaning |
|---|---|
| `-o DIR` | output directory |
| `-t TEMPLATE` | URL template containing `{id}` |
| `--html FILE` / `--base-url URL` | parse a saved page instead of fetching one |
| `-l`, `--list` [`--json`] | print URLs only |
| `--strip-resize` | remove CDN resize params (`?w=`, `?h=`, …) to request originals |
| `--no-og` | ignore `og:image` meta tags |
| `--min-bytes N`, `--min-dim N` | size filters |
| `-H 'Name: value'` | extra request header |
| `-j N` | parallel downloads (default 6) |
| `--timeout S`, `--retries N` | network tuning |

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run the real CLI end to end against a local HTTP server that serves a sample listing page.
