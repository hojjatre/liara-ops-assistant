"""Fake Liara responses shaped like the official OpenAPI spec / CLI types."""
import time

NOW = int(time.time())

ME = {
    "user": {"email": "x@example.com"},
    "plans": {
        "projects": {
            "free": {"RAM": {"amount": 0.25}, "CPU": {"amount": 0.1}, "volume": 1, "price": 0, "available": True,
                     "region": "iran"},
            "g2-earth": {"RAM": {"amount": 0.5}, "CPU": {"amount": 0.5}, "volume": 5, "price": 250,
                         "available": True, "region": "iran"},
            "g2-mars": {"RAM": {"amount": 1}, "CPU": {"amount": 1}, "volume": 10, "price": 500, "available": True,
                        "region": "iran"},
            "g2-jupiter": {"RAM": {"amount": 2}, "CPU": {"amount": 1}, "volume": 20, "price": 1000,
                           "available": True, "region": "iran"},
            "old-plan": {"RAM": {"amount": 4}, "CPU": {"amount": 2}, "volume": 40, "price": 1500,
                         "available": False, "region": "iran"},
        },
        "projectBundlePlans": {
            "g2-earth": {"free": {"displayPrice": 0}, "basic": {"displayPrice": 100000},
                         "standard": {"displayPrice": 300000}, "pro": {"displayPrice": 900000}},
        },
    },
}


def project(scale=1, envs=None, plan="g2-earth"):
    return {"project": {"_id": "p123", "project_id": "demo-shop", "type": "node", "status": "ACTIVE",
                        "scale": scale, "planID": plan, "isDeployed": True, "zeroDowntime": False,
                        "envs": envs if envs is not None else [{"key": "NODE_ENV", "value": "production"}]}}


def releases(created_minutes_ago=10):
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - created_minutes_ago * 60))
    older = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 86400))
    return {"total": 2, "currentRelease": "r2", "releases": [
        {"_id": "r2", "tag": "v2", "state": "READY", "port": 3000, "created_at": created},
        {"_id": "r1", "tag": "v1", "state": "READY", "port": 3000, "created_at": older},
    ]}


def logs_missing_env(minutes_ago=5):
    base = (NOW - minutes_ago * 60) * 10**9
    lines = []
    for i in range(12):
        ts = str(base + i * 10 * 10**9)
        lines.append([ts, "2026-10-22T06:11:34.136Z [ERROR] DATABASE_URL is not set — cannot connect to the "
                          "orders database"])
        lines.append([ts, "    at getOrders (/app/index.js:44:11)"])
        lines.append([ts, f"2026-10-22T06:11:34.140Z [ERROR] GET / 500 {3 + i}ms rss=68MB"])
    return {"data": [{"metaData": {"releaseId": "r2"}, "values": lines}]}


def logs_quiet():
    base = (NOW - 120) * 10**9
    return {"data": [{"metaData": {"releaseId": "r2"},
                      "values": [[str(base), "2026-10-22T06:11:34Z [INFO] GET / 200 900ms rss=480MB"]]}]}


def summary(mem_bytes=60 * 1024 * 1024, cpu_cores=0.05):
    return {"cpuUsage": [{"value": [NOW, str(cpu_cores)], "applet": "a"}],
            "memoryUsage": [{"value": [NOW, str(mem_bytes)], "applet": "a"}],
            "disksUsage": []}


def series(values):
    return {"end": NOW, "result": [{"applet": "a", "values": [[NOW - (len(values) - i) * 60, str(v)]
                                                              for i, v in enumerate(values)]}]}


def events(texts):
    return {"data": [{"metaData": {"type": "app"}, "values": [[str((NOW - 300 + i) * 10**9), t]
                                                             for i, t in enumerate(texts)]}]}
