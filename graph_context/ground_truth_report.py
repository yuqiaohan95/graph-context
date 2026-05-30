#!/usr/bin/env python3
"""Ground-truth evaluation report writer (CSV)."""
import csv
from typing import Dict, Any


def save_csv_report(result: Dict[str, Any], path: str) -> None:
    """Write a simple CSV report from the evaluation results."""
    rows = []
    per_query = result.get('per_query', []) if isinstance(result, dict) else []
    for item in per_query:
        rows.append({
            'query': item.get('query'),
            'top_k': item.get('top_k'),
            'hits': item.get('hits'),
            'recall': item.get('recall'),
            'precision': item.get('precision'),
        })

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['query','top_k','hits','recall','precision'])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
