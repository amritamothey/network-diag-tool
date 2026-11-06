import ast, collections, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, '..', 'results', 'repeats.log')

data = collections.defaultdict(list)
for line in open(LOG):
    label, _, rest = line.partition('|')
    m = re.search(r'\{.*?\}', rest)
    if not m:
        continue
    try:
        d = ast.literal_eval(m.group(0))
    except Exception:
        continue
    pc = re.search(r'path changes:\s*(\d+)', rest)
    if pc:
        d['path_changes'] = int(pc.group(1))
    data[label.strip()].append(d)

out = ['| Experiment | Runs | Metric | Average | Min | Max |', '|---|---|---|---|---|---|']
for label, runs in data.items():
    for k in sorted({k for r in runs for k in r}):
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if vals:
            out.append(f'| {label} | {len(runs)} | {k} | {sum(vals)/len(vals):.2f} | {min(vals):.2f} | {max(vals):.2f} |')

text = '\n'.join(out)
print(text)
open(os.path.join(HERE, '..', 'results', 'summary.md'), 'w').write(text + '\n')
