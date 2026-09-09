"""Runtime patch for reliable geocoding of known Sugi Pharmacy help stores.

The main synchronizer intentionally keeps generic Nominatim lookup logic.  This
module is imported by the workflow immediately before rshift_sync.py and rewrites
ambiguous store-name searches to the verified public store address.
"""

import requests

STORE_ADDRESS_QUERIES = {
    "ことぶき店": "〒486-0831 愛知県春日井市ことぶき町8番地3",
    "高蔵寺店": "〒487-0013 愛知県春日井市高蔵寺町1丁目46番地",
    "篠木店": "〒486-0851 愛知県春日井市篠木町7丁目45番地23",
    "大手店": "〒486-0807 愛知県春日井市大手町3丁目21番地6",
}

_original_get = requests.get


def _patched_get(url, *args, **kwargs):
    if "nominatim.openstreetmap.org/search" in str(url):
        params = dict(kwargs.get("params") or {})
        query = str(params.get("q") or "")
        for store, address in STORE_ADDRESS_QUERIES.items():
            if store in query:
                params["q"] = address
                kwargs["params"] = params
                break
    return _original_get(url, *args, **kwargs)


requests.get = _patched_get
