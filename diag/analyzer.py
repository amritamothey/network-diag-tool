#!/usr/bin/env python3
import csv, json, os, re, subprocess, sys, threading, time, urllib.request
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, '..', 'topology'))
from diamond_topo import build_net
from mininet.log import setLogLevel

CTRL = 'http://127.0.0.1:8080'
SRC, DST = '10.0.0.1', '10.0.0.2'
RESULTS = os.path.join(ROOT, '..', 'results')
EXPECTED_ONE_WAY_MS = {'s1-s2-s4': 10, 's1-s3-s4': 40}


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CTRL + path, data=data, headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=5))


def start(net):
    net.start()
    time.sleep(6)
    net.get('h2').cmd(f'python3 {ROOT}/probe_server.py 9000 > /tmp/server.log 2>&1 &')
    net.pingAll()
    net.pingAll()
    time.sleep(1)


def flow_packets(switch, src=SRC, dst=DST):
    out = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', switch],
                         capture_output=True, text=True).stdout
    return sum(int(re.search(r'n_packets=(\d+)', l).group(1))
               for l in out.splitlines() if f'nw_src={src},nw_dst={dst}' in l)


def probe_window(net, count, interval, timeout=0.5, size=64, tag='w'):
    log = f'/tmp/{tag}.csv'
    out = net.get('h1').cmd(f'python3 {ROOT}/probe_client.py --dst {DST} --count {count} '
                            f'--interval {interval} --timeout {timeout} --size {size} --log {log}')
    summary = json.loads(out.strip().splitlines()[-1])
    with open(log) as f:
        rows = [(float(r['sent_at']), float(r['rtt_ms']) if r['rtt_ms'] else None)
                for r in csv.DictReader(f)]
    return summary, rows


def run_windows(net, windows, count=20, interval=0.05, report=True, faults=()):
    for delay, fn in faults:
        threading.Timer(delay, fn).start()
    records, all_rows = [], []
    for i in range(windows):
        before = api(f'/path?src={SRC}&dst={DST}')['active_path']
        summary, rows = probe_window(net, count, interval, tag=f'w{i}')
        after = api(f'/path?src={SRC}&dst={DST}')['active_path']
        records.append({'t': time.time(), 'path': before, 'path_changed': before != after, **summary})
        all_rows += [(t, rtt, before) for t, rtt in rows]
        if report and before and before == after:
            api('/report', {'src': SRC, 'dst': DST, 'path': before,
                            'rtt_avg': summary.get('rtt_avg'), 'loss_pct': summary['loss_pct'],
                            'jitter': summary.get('jitter')})
    return records, all_rows


def verify_path(net):
    sw = ['s1', 's2', 's3', 's4']
    before = {s: flow_packets(s) for s in sw}
    probe_window(net, 20, 0.05, tag='verify')
    return {s: flow_packets(s) - before[s] for s in sw}


def save_csv(records, fname):
    os.makedirs(RESULTS, exist_ok=True)
    with open(fname, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in records for k in r}))
        w.writeheader()
        w.writerows(records)


def plot(rows, marks, title, fname):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    t0 = rows[0][0]
    ok = [(t - t0, r) for t, r, _ in rows if r is not None]
    lost = [t - t0 for t, r, _ in rows if r is None]
    plt.figure(figsize=(9, 4))
    plt.plot([x for x, _ in ok], [y for _, y in ok], '.', ms=3, label='RTT of replied probes')
    plt.plot(lost, [0] * len(lost), 'rx', label='lost probe')
    for name, t in marks.items():
        plt.axvline(t - t0, ls='--', color='gray')
        plt.text(t - t0, plt.ylim()[1] * 0.9, name)
    plt.xlabel('time (s)')
    plt.ylabel('RTT (ms)')
    plt.title(title)
    plt.legend()
    plt.tight_layout()
    plt.savefig(fname)


def outage_stats(rows, t_down):
    after = [(t, r) for t, r, _ in rows if t >= t_down]
    first_loss = next((t for t, r in after if r is None), None)
    if first_loss is None:
        return {'outage_s': 0, 'lost_probes': 0}
    recovered = next((t for t, r in after if t > first_loss and r is not None), None)
    return {'outage_s': round(recovered - first_loss, 2) if recovered else None,
            'lost_probes': sum(1 for t, r in after if r is None)}


def aggregate(recs):
    got = sum(r['received'] for r in recs)
    sent = sum(r['sent'] for r in recs)
    vals = [r for r in recs if 'rtt_avg' in r]
    return {'sent': sent, 'loss_pct': round(100 * (sent - got) / sent, 2),
            'rtt_avg': round(sum(r['rtt_avg'] * r['received'] for r in vals) / max(got, 1), 2),
            'rtt_max': max((r['rtt_max'] for r in vals), default=None),
            'jitter': round(sum(r['jitter'] for r in vals) / max(len(vals), 1), 2)}


def start_load(net, mbps=20, secs=40):
    net.get('h4').cmd('iperf3 -s -D')
    net.get('h3').cmd(f'iperf3 -c 10.0.0.4 -u -b {mbps}M -t {secs} > /tmp/iperf.log 2>&1 &')


# ---------------- experiments ----------------
def exp_correlate(net):
    recs, _ = run_windows(net, windows=5)
    for r in recs:
        exp = 2 * EXPECTED_ONE_WAY_MS.get(r['path'], 0)
        print(f"{r['path']}: avg RTT {r.get('rtt_avg')} ms (expected about {exp}), loss {r['loss_pct']}%")
    print('switch counters moved:', verify_path(net))


def exp_failure(net):
    marks = {}

    def down():
        marks['link_down'] = time.time()
        net.configLinkStatus('s1', 's2', 'down')

    def up():
        marks['link_up'] = time.time()
        net.configLinkStatus('s1', 's2', 'up')

    recs, rows = run_windows(net, windows=25, faults=[(8, down), (22, up)])
    stats = outage_stats(rows, marks['link_down'])
    ev = api('/events')
    failed = next((e['time'] for e in ev if e['event'] == 'PATH_FAILED'), None)
    rerouted = next((e['time'] for e in ev if e['event'] == 'PATH_INSTALLED'
                     and e.get('reason') == 'LINK_FAILURE'), None)
    stats['detect_s'] = round(failed - marks['link_down'], 3) if failed else None
    stats['reroute_s'] = round(rerouted - marks['link_down'], 3) if rerouted else None
    print('FAILURE RESULT:', stats)
    tag = 'static' if '--static' in sys.argv else 'dynamic'
    plot(rows, marks, f'Link s1-s2 failure ({tag})', os.path.join(RESULTS, f'failure_{tag}.png'))
    save_csv(recs, os.path.join(RESULTS, f'failure_{tag}_windows.csv'))


def exp_compare(net):
    ids = [p['id'] for p in api(f'/path?src={SRC}&dst={DST}')['paths']]
    table = []
    for condition in ('idle', 'loaded'):
        if condition == 'loaded':
            start_load(net)
            time.sleep(3)
        for pid in ids:
            api('/reroute', {'src': SRC, 'dst': DST, 'path': pid})
            time.sleep(1)
            recs, _ = run_windows(net, windows=8, count=30, report=False)
            table.append({'condition': condition, 'path': pid, **aggregate(recs)})
    api('/reroute', {'src': SRC, 'dst': DST, 'path': 'auto'})
    print(f"{'condition':9} {'path':10} {'avg RTT':>8} {'max RTT':>8} {'jitter':>7} {'loss%':>6}")
    for r in table:
        print(f"{r['condition']:9} {r['path']:10} {r['rtt_avg']:8} {r['rtt_max']:8} {r['jitter']:7} {r['loss_pct']:6}")
    save_csv(table, os.path.join(RESULTS, 'compare.csv'))


def exp_congestion(net):
    marks = {}

    def load():
        marks['load_start'] = time.time()
        start_load(net, mbps=20, secs=25)

    recs, rows = run_windows(net, windows=25, faults=[(5, load)])
    print('CONGESTION:', aggregate(recs), 'path changes:', sum(1 for a, b in zip(recs, recs[1:]) if a['path'] != b['path']))
    tag = 'static' if '--static' in sys.argv else 'dynamic'
    plot(rows, marks, f'Congestion on path A ({tag})', os.path.join(RESULTS, f'congestion_{tag}.png'))
    save_csv(recs, os.path.join(RESULTS, f'congestion_{tag}_windows.csv'))


if __name__ == '__main__':
    setLogLevel('warning')
    scenario = sys.argv[1]
    net = build_net()
    start(net)
    try:
        if '--static' in sys.argv:
            api('/mode', {'dynamic': False})
        globals()['exp_' + scenario](net)
    finally:
        net.stop()
