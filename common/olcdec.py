"""Plus code -> (lat, lng) for Buôn Ma Thuột. Accepts full codes ('7P4CM2HW+XRP'), local codes ('M2HW+XRP',
prefixed with 7P4C) and short codes ('HW+XR', recovered near BMT). Returns None for anything unusable."""
import re
from openlocationcode import openlocationcode as _olc

PREFIX = '7P4C'
REF = (12.68, 108.04)          # Buôn Ma Thuột


def decode(c):
    try:
        c = re.sub(r'\s+', '', str(c or '')).upper()
        m = re.search(r'[23456789CFGHJMPQRVWX]*\+[23456789CFGHJMPQRVWX]*', c)
        if not m:
            return None
        c = m.group(0)
        cands = [c]
        if c.startswith(PREFIX + PREFIX):
            cands.insert(0, c[len(PREFIX):])
        elif c.startswith(PREFIX) and c.index('+') != 8:
            cands = [c[len(PREFIX):]]              # '7P4C' + already-short code
        for k in cands:
            head, _, tail = k.partition('+')
            if len(tail) == 1 or (len(head) == 8 and not tail) or len(head) not in (2, 4, 6, 8):
                continue
            if len(head) == 8 and _olc.isFull(k):
                a = _olc.decode(k)
            elif len(head) < 8 and tail and _olc.isShort(k):
                a = _olc.decode(_olc.recoverNearest(k, *REF))
            else:
                continue
            return round(a.latitudeCenter, 7), round(a.longitudeCenter, 7)
    except Exception:  # noqa: BLE001 - bad input never crashes the caller
        return None
    return None


if __name__ == '__main__':
    import sys
    print(decode(sys.argv[1] if len(sys.argv) > 1 else ''))
