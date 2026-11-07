<?php
/**
 * mls-grab web front end: enter an address, get the listing photos (gallery + ZIP).
 * Runs the Python CLI; no PHP extensions beyond the defaults are needed.
 */

// ---- configuration (install.sh writes these into config.php) -----------------------------
$cfg = [
    'python'   => '/usr/bin/python3',
    'app_dir'  => '/opt/mls-grab',          // checkout containing the mls_grab/ package
    'jobs_dir' => __DIR__ . '/jobs',        // must be writable by the PHP-FPM user
    'jobs_url' => 'jobs',                   // URL path nginx serves jobs_dir from
    'timeout'  => 240,                      // seconds for one lookup + download
];
if (is_file(__DIR__ . '/config.php')) {
    $cfg = array_merge($cfg, require __DIR__ . '/config.php');
}

function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }

function run_grab(array $cfg, string $address, string $outDir): array {
    $cmd = [$cfg['python'], '-m', 'mls_grab', $address, '--address', '--json', '--zip',
            '-o', $outDir];
    $env = ['PYTHONPATH' => $cfg['app_dir'], 'PATH' => '/usr/local/bin:/usr/bin:/bin',
            'LANG' => 'C.UTF-8'];
    foreach (['HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'NO_PROXY'] as $k) {
        if (getenv($k) !== false) $env[$k] = getenv($k);
    }
    $proc = proc_open($cmd, [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, $cfg['app_dir'], $env);
    if (!is_resource($proc)) return ['ok' => false, 'error' => 'could not start python'];
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);
    $out = $err = '';
    $deadline = time() + $cfg['timeout'];
    while (true) {
        $r = [$pipes[1], $pipes[2]]; $w = $e = null;
        if (stream_select($r, $w, $e, 1) === false) break;
        foreach ($r as $p) {
            $chunk = fread($p, 65536);
            if ($p === $pipes[1]) $out .= $chunk; else $err .= $chunk;
        }
        $st = proc_get_status($proc);
        if (!$st['running']) {
            $out .= stream_get_contents($pipes[1]);
            $err .= stream_get_contents($pipes[2]);
            break;
        }
        if (time() > $deadline) {
            proc_terminate($proc);
            fclose($pipes[1]); fclose($pipes[2]); proc_close($proc);
            return ['ok' => false, 'error' => 'timed out', 'log' => $err];
        }
    }
    fclose($pipes[1]); fclose($pipes[2]); proc_close($proc);
    $res = json_decode($out, true);
    if (!is_array($res)) $res = ['ok' => false, 'error' => 'unexpected output from mls-grab'];
    $res['log'] = $err;
    return $res;
}

function recent_jobs(array $cfg, int $limit = 15): array {
    $jobs = [];
    foreach (glob($cfg['jobs_dir'] . '/*/sources.json') ?: [] as $f) {
        $m = json_decode((string)file_get_contents($f), true) ?: [];
        $jobs[] = ['id' => basename(dirname($f)), 'time' => filemtime($f),
                   'address' => $m['address'] ?? '?', 'n' => count($m['photos'] ?? []),
                   'provider' => $m['provider'] ?? ''];
    }
    usort($jobs, fn($a, $b) => $b['time'] <=> $a['time']);
    return array_slice($jobs, 0, $limit);
}

// ---- request handling ---------------------------------------------------------------------
$result = null; $job = null; $address = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $address = trim((string)($_POST['address'] ?? ''));
    if ($address === '' || strlen($address) > 200 || !preg_match('/^\d+\w?\s+\S/u', $address)) {
        $result = ['ok' => false, 'error' => 'Enter a full street address, e.g. "1 Tribou Drive, Paris, ME 04281".'];
    } else {
        if (!is_dir($cfg['jobs_dir'])) @mkdir($cfg['jobs_dir'], 0775, true);
        $job = date('Ymd-His') . '-' . substr(bin2hex(random_bytes(4)), 0, 8);
        @set_time_limit($cfg['timeout'] + 30);
        $result = run_grab($cfg, $address, $cfg['jobs_dir'] . '/' . $job);
    }
} elseif (isset($_GET['job']) && preg_match('/^[0-9A-Za-z-]{8,40}$/', $_GET['job'])) {
    $job = $_GET['job'];
    $src = $cfg['jobs_dir'] . "/$job/sources.json";
    if (is_file($src)) {
        $m = json_decode(file_get_contents($src), true) ?: [];
        $result = ['ok' => true, 'address' => $m['address'] ?? '', 'provider' => $m['provider'] ?? '',
                   'listing_url' => $m['listing_url'] ?? '', 'status' => $m['status'] ?? '',
                   'files' => $m['photos'] ?? [], 'saved' => count($m['photos'] ?? []),
                   'zip' => is_file($cfg['jobs_dir'] . "/$job/photos.zip") ? 'photos.zip' : null];
    }
}
$base = rtrim($cfg['jobs_url'], '/') . '/' . rawurlencode((string)$job);
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listing Photos</title>
<style>
  :root { --bg:#f6f7f9; --fg:#1b1f24; --muted:#5b6470; --card:#fff; --line:#dde1e6; --accent:#2563eb; --bad:#b42318; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111418; --fg:#e7eaee; --muted:#9aa4b1; --card:#1a1e24; --line:#2b313a; --accent:#6ea0ff; --bad:#ff8a80; } }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:1.4rem; margin:0 0 16px; }
  form { display:flex; gap:8px; flex-wrap:wrap; }
  input[type=text] { flex:1 1 320px; padding:12px 14px; font-size:1rem; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); }
  button, .btn { padding:12px 18px; font-size:1rem; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; text-decoration:none; display:inline-block; }
  button[disabled] { opacity:.6; cursor:wait; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-top:20px; }
  .muted { color:var(--muted); font-size:.9rem; }
  .err { color:var(--bad); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:10px; margin-top:16px; }
  .grid a { display:block; aspect-ratio:4/3; overflow:hidden; border-radius:8px; background:var(--line); }
  .grid img { width:100%; height:100%; object-fit:cover; display:block; }
  table { width:100%; border-collapse:collapse; font-size:.95rem; }
  td { padding:6px 4px; border-top:1px solid var(--line); }
  details pre { white-space:pre-wrap; font-size:.8rem; color:var(--muted); }
  ul.attempts { margin:8px 0 0; padding-left:20px; }
</style>
</head>
<body>
<main>
  <h1>Listing photos by address</h1>
  <form method="post" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Searching…'">
    <input type="text" name="address" required maxlength="200" autofocus
           placeholder="1 Tribou Drive, Paris, ME 04281" value="<?= h($address) ?>">
    <button type="submit">Get photos</button>
  </form>
  <p class="muted">Looks the address up on Redfin, Realtor.com and Zillow, then downloads every listing photo. This can take up to a minute.</p>

<?php if ($result): ?>
  <section class="card">
  <?php if (!empty($result['ok'])): ?>
    <h2 style="margin:0 0 4px;font-size:1.15rem"><?= h($result['address'] ?? '') ?></h2>
    <p class="muted" style="margin:0">
      <?= (int)($result['saved'] ?? 0) ?> photos from <?= h($result['provider'] ?? '') ?>
      <?= !empty($result['status']) ? '· ' . h($result['status']) : '' ?>
      <?php if (!empty($result['listing_url'])): ?>· <a href="<?= h($result['listing_url']) ?>" target="_blank" rel="noopener noreferrer">listing</a><?php endif; ?>
      · <a href="?job=<?= h($job) ?>">permalink</a>
    </p>
    <?php if (!empty($result['zip'])): ?>
      <p><a class="btn" href="<?= h($base) ?>/photos.zip" download>Download all (ZIP)</a></p>
    <?php endif; ?>
    <div class="grid">
      <?php foreach ($result['files'] ?? [] as $f): $u = $base . '/' . rawurlencode($f['file']); ?>
        <a href="<?= h($u) ?>" target="_blank"><img src="<?= h($u) ?>" loading="lazy" alt="<?= h($f['file']) ?>"></a>
      <?php endforeach; ?>
    </div>
  <?php else: ?>
    <p class="err"><strong>No photos.</strong> <?= h($result['error'] ?? 'Unknown error') ?></p>
    <?php if (!empty($result['attempts'])): ?>
      <ul class="attempts">
        <?php foreach ($result['attempts'] as $a): ?>
          <li><strong><?= h($a['provider']) ?></strong>: <?= h($a['outcome']) ?> — <span class="muted"><?= h($a['message']) ?></span></li>
        <?php endforeach; ?>
      </ul>
    <?php endif; ?>
  <?php endif; ?>
  <?php if (!empty($result['log'])): ?>
    <details><summary class="muted">Log</summary><pre><?= h($result['log']) ?></pre></details>
  <?php endif; ?>
  </section>
<?php endif; ?>

<?php $recent = recent_jobs($cfg); if ($recent): ?>
  <section class="card">
    <h2 style="margin:0 0 8px;font-size:1.05rem">Recent</h2>
    <table>
      <?php foreach ($recent as $r): ?>
        <tr><td><a href="?job=<?= h($r['id']) ?>"><?= h($r['address']) ?></a></td>
            <td class="muted"><?= (int)$r['n'] ?> photos · <?= h($r['provider']) ?></td>
            <td class="muted"><?= h(date('Y-m-d H:i', $r['time'])) ?></td></tr>
      <?php endforeach; ?>
    </table>
  </section>
<?php endif; ?>
</main>
</body>
</html>
