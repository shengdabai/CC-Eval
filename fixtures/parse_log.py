import sys

def parse(line):
    parts = line.split(",")
    ts = parts[0]
    level = parts[1]
    msg = parts[2]
    return {"ts": ts, "level": level, "msg": msg}

def summarize(path):
    counts = {}
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        rec = parse(line)
        counts[rec["level"]] = counts.get(rec["level"], 0) + 1
    return counts

if __name__ == "__main__":
    print(summarize(sys.argv[1]))
