"""
Harga pasaran ikan (runcit) Negeri Perak daripada KPDN PriceCatcher (data.gov.my).

Dijalankan setiap hari oleh GitHub Actions. Menghasilkan data/harga_ikan_perak.json
yang kecil untuk dibaca oleh Sistem Jualan Ikan Murah LKIM Perak (Apps Script).

Sumber: https://open.dosm.gov.my/data-catalogue/pricecatcher (data terbuka kerajaan).
"""
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

ASAS = "https://storage.data.gov.my/pricecatcher"
NEGERI = "perak"
TEMPOH_HARI = 14          # tetingkap median
KATA_KUNCI = re.compile(r"\b(?:IKAN|UDANG|SOTONG|KETAM|KERANG|LALA|SIPUT|TIRAM|KUPANG|CUMI)\b", re.I)
MYT = timezone(timedelta(hours=8))


def muat(url, jenis):
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    if jenis == "parquet":
        return pd.read_parquet(io.BytesIO(r.content))
    return pd.read_csv(io.BytesIO(r.content))


def muat_lookup(nama):
    try:
        return muat(f"{ASAS}/{nama}.parquet", "parquet")
    except Exception:
        return muat(f"{ASAS}/{nama}.csv", "csv")


def kg_daripada_unit(unit):
    """Tukar unit PriceCatcher kepada kg. Pulangkan None jika bukan berat."""
    if not isinstance(unit, str):
        return None
    u = unit.strip().lower().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)?(kg|kilogram|g|gram)$", u)
    if not m:
        return None
    n = float(m.group(1) or 1)
    return n if m.group(2) in ("kg", "kilogram") else n / 1000.0


POSKOD_BANDAR = re.compile(r"\b\d{5}\s*,?\s*([A-Za-z][A-Za-z .'()/-]*?)\s*(?:,|$)")


def bandar_daripada_alamat(alamat):
    """Ambil nama bandar selepas poskod 5 digit (cth '34200 PARIT BUNTAR, PERAK' -> 'PARIT BUNTAR')."""
    if not isinstance(alamat, str):
        return None
    m = POSKOD_BANDAR.search(alamat.upper())
    if not m:
        return None
    b = re.sub(r"\s+", " ", m.group(1)).strip(" .-")
    b = re.sub(r"(\s+(DARUL RIDZUAN|PERAK|MALAYSIA))+$", "", b).strip(" .-")
    if not b or b in ("PERAK", "MALAYSIA"):
        return None
    return b


def main():
    kini = datetime.now(MYT)
    bulan = [kini.strftime("%Y-%m")]
    if kini.day <= TEMPOH_HARI + 1:
        bulan.append((kini.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))

    item = muat_lookup("lookup_item")
    premis = muat_lookup("lookup_premise")

    item = item[item["item"].astype(str).str.contains(KATA_KUNCI)].copy()
    item["kg"] = item["unit"].map(kg_daripada_unit)
    item = item[item["kg"].notna() & (item["kg"] > 0)]

    premis = premis[premis["state"].astype(str).str.strip().str.lower() == NEGERI].copy()
    if "address" in premis.columns:
        premis["bandar"] = premis["address"].map(bandar_daripada_alamat)
    else:
        print("Amaran: lookup_premise tiada lajur address; harga ikut bandar dilangkau.", file=sys.stderr)
        premis["bandar"] = None
    print(f"Premis Perak: {len(premis)}, bandar dikenal pasti: {premis['bandar'].notna().sum()}")
    premis = premis[["premise_code", "district", "premise_type", "bandar"]]

    bingkai = []
    for b in bulan:
        try:
            df = muat(f"{ASAS}/pricecatcher_{b}.parquet", "parquet")
        except requests.HTTPError as e:
            print(f"Amaran: {b} tidak tersedia ({e})", file=sys.stderr)
            continue
        df = df[df["item_code"].isin(item["item_code"]) & df["premise_code"].isin(premis["premise_code"])]
        bingkai.append(df)

    if not bingkai:
        print("Tiada data PriceCatcher.", file=sys.stderr)
        sys.exit(1)

    df = pd.concat(bingkai, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    akhir = df["date"].max()
    df = df[df["date"] > akhir - pd.Timedelta(days=TEMPOH_HARI)]
    df = df.merge(item[["item_code", "item", "unit", "kg"]], on="item_code").merge(premis, on="premise_code")
    df["harga_kg"] = df["price"] / df["kg"]
    df = df[(df["harga_kg"] > 0) & (df["harga_kg"] < 1000)]

    def ringkas(g):
        return pd.Series({
            "median": round(float(g["harga_kg"].median()), 2),
            "min": round(float(g["harga_kg"].min()), 2),
            "max": round(float(g["harga_kg"].max()), 2),
            "bil": int(len(g)),
            "premis": int(g["premise_code"].nunique()),
            "tarikh_terkini": g["date"].max().strftime("%Y-%m-%d"),
        })

    ikut_daerah = df.groupby(["item_code", "district"]).apply(ringkas, include_groups=False).reset_index()
    ikut_negeri = df.groupby(["item_code"]).apply(ringkas, include_groups=False).reset_index()
    df_b = df[df["bandar"].notna()]
    ikut_bandar = (df_b.groupby(["item_code", "district", "bandar"]).apply(ringkas, include_groups=False).reset_index()
                   if len(df_b) else pd.DataFrame(columns=["item_code", "district", "bandar"]))

    keluaran = {
        "sumber": "KPDN PriceCatcher melalui data.gov.my (data terbuka kerajaan)",
        "negeri": "Perak",
        "dijana": kini.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_hingga": akhir.strftime("%Y-%m-%d"),
        "tempoh_hari": TEMPOH_HARI,
        "item": [
            {"kod": int(r.item_code), "nama": str(r.item), "unit": str(r.unit)}
            for r in item[item["item_code"].isin(df["item_code"])].itertuples()
        ],
        "daerah": [
            {"kod": int(r.item_code), "daerah": str(r.district), "median": r.median, "min": r.min, "max": r.max,
             "bil": r.bil, "premis": r.premis, "tarikh_terkini": r.tarikh_terkini}
            for r in ikut_daerah.itertuples()
        ],
        "bandar": [
            {"kod": int(r.item_code), "daerah": str(r.district), "bandar": str(r.bandar), "median": r.median,
             "min": r.min, "max": r.max, "bil": r.bil, "premis": r.premis, "tarikh_terkini": r.tarikh_terkini}
            for r in ikut_bandar.itertuples()
        ],
        "negeri_median": [
            {"kod": int(r.item_code), "median": r.median, "min": r.min, "max": r.max,
             "bil": r.bil, "premis": r.premis, "tarikh_terkini": r.tarikh_terkini}
            for r in ikut_negeri.itertuples()
        ],
    }

    os.makedirs("data", exist_ok=True)
    with open("data/harga_ikan_perak.json", "w", encoding="utf-8") as f:
        json.dump(keluaran, f, ensure_ascii=False, indent=1)
    print(f"Siap: {len(keluaran['item'])} item, {len(keluaran['daerah'])} baris daerah, "
          f"{len(keluaran['bandar'])} baris bandar, data hingga {keluaran['data_hingga']}.")


if __name__ == "__main__":
    main()
