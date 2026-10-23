# -*- coding: utf-8 -*-
"""CC-CEDICT 词典查询（从原 panel.py 提取，接口不变）。"""
import re


class CEDict:
    def __init__(self, path):
        self.m = {}
        self.load(path)

    def load(self, path):
        rx = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]+)\]\s+/(.*)/\s*$")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                mt = rx.match(line)
                if not mt:
                    continue
                simp = mt.group(2)
                py = mt.group(3)
                defs = [d for d in mt.group(4).split("/") if d]
                if not defs:
                    continue
                self.m.setdefault(simp, []).append((py, defs))

    def gloss(self, word):
        ent = self.m.get(word)
        if ent:
            _, defs = ent[0]
            if defs:
                return defs[0]
        return None

    def glosses(self, word, limit=3):
        """返回前几个释义（英文主角，可能多义）。"""
        ent = self.m.get(word)
        if not ent:
            return []
        out = []
        for _, defs in ent:
            for d in defs:
                if d not in out:
                    out.append(d)
                if len(out) >= limit:
                    return out
        return out
