# mls-grab

Downloads every photo of a home listing from its **street address** (or a listing URL). It needs only Python 3.9+ and no other packages. It comes with an optional password-protected web page for an nginx + PHP server.

```bash
# By address: tries Redfin, then Realtor.com, then Zillow, and uses the first with photos
mls-grab '1 Tribou Drive, Paris, ME 04281'
mls-grab '1 Tribou Drive, Paris, ME 04281' --zip --json   # ZIP + machine-readable summary
mls-grab '...' --providers realtor,zillow                  # choose the sites and their order

pip install .            # installs the `mls-grab` command (or use: python -m mls_grab)

# By listing page URL
mls-grab https://your-mls-site.example/listing/1683368

# By bare listing ID, with a URL template for your MLS site
mls-grab 1683368 --url-template 'https://your-mls-site.example/listing/{id}'
export MLS_GRAB_URL_TEMPLATE='https://your-mls-site.example/listing/{id}'   # set once
mls-grab 1683368

# Zillow: full URL, or just the zpid
mls-grab https://www.zillow.com/homedetails/1-Tribou-Drive-Paris-ME-04281/304949881_zpid
mls-grab 304949881 --site zillow

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

## Address lookup: how it works and its limits

| site | finds the home with | gets photos from | status from a datacenter IP (tested Nov 2026) |
|---|---|---|---|
| Redfin | `stingray/do/location-autocomplete` | `stingray/api/home/details/aboveTheFold`, else the home page | **blocked** (CloudFront 403) |
| Realtor.com | `parser-external.geo.moveaws.com/suggest` | GraphQL `home(property_id){photos}` | lookup **works**; photo query **unverified** (502) |
| Zillow | `zillowstatic.com/autocomplete/v3/suggestions` | listing page `__NEXT_DATA__` | lookup **works**; page **blocked** (captcha) |

These are the sites' own web endpoints. They are not public APIs, and they change and block automated traffic without notice. Each attempt is reported (`ok`, `not_found`, `no_photos`, `blocked`, `error`), so you can see which site failed and why. The tool never tries to get around a captcha or a block.

## Web front end (nginx + PHP)

```bash
git clone <this repo> && cd mls-grab
sudo ./deploy/install.sh photos.example.com admin    # prints the generated password
sudo certbot --nginx -d photos.example.com           # optional HTTPS
```

The installer:

- copies the package to `/opt/mls-grab` and adds a `mls-grab` command to `/usr/local/bin`
- installs the page to `/var/www/mls-grab`, with downloads kept in `/var/www/mls-grab/jobs`
- adds an nginx site protected by basic auth, a PHP-FPM socket it detects automatically, and `fastcgi_read_timeout 300`
- adds a daily cron job that deletes downloads older than 14 days

The page takes an address, shows the photos as a gallery, and offers a ZIP download, a permalink and a list of recent lookups. It runs `python3 -m mls_grab ... --json --zip` with `proc_open`, passing the address as an argument array rather than through a shell.

## Site adapters

Some sites get an adapter that reads the listing's own data instead of scanning the whole page. This matters on portals, where a page also shows photos of nearby and similar homes.

- **Zillow** reads the property record embedded in `__NEXT_DATA__` (inside `gdpClientCache`). It keeps only the photos of the home whose `zpid` matches the URL, and takes the largest JPEG of each one. Zillow often answers scripts with a "Press & Hold" captcha. When it does, mls-grab says so; use the saved-page route below. Photo downloads from `photos.zillowstatic.com` don't hit the captcha.

`--generic` turns adapters off.

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
python -m unittest discover -s . -p 'test_*.py' -t . -v
```

The tests run the real CLI end to end against a local HTTP server that serves a sample listing page.
