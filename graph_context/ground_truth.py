#!/usr/bin/env python3
"""Ground-truth evaluation utilities for MCP context experiments."""
import json
from typing import List, Dict, Any


class GroundTruthEvaluator:
    @staticmethod
    def load_gt(path: str) -> List[Dict[str, Any]]:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # support both { queries: [...] } and { "queries": [...] }
        if isinstance(data, dict) and 'queries' in data:
            return data['queries']
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def evaluate(engine: Any, gt_path: str, top_k: int = 5, idf_override: bool | None = None) -> Dict[str, Any]:
        # Optionally override IDF boost setting for this evaluation
        restored = None
        if idf_override is not None and hasattr(engine, 'config'):
            restored = engine.config.get('idf_boost_enabled', True)
            engine.config['idf_boost_enabled'] = bool(idf_override)
        queries = GroundTruthEvaluator.load_gt(gt_path)
        if not queries:
            return {"error": "No ground-truth queries found"}

        total = len(queries)
        per_query = []
        hits_total = 0
        for q in queries:
            query_text = q.get('query') or q.get('text') or ''
            qtop = int(q.get('top_k', top_k))
            results = engine.retrieve(query_text, top_k=qtop)

            # determine hits using explicit 'relevant' and 'relevant_contains'
            relevant = q.get('relevant', [])
            relevant_contains = q.get('relevant_contains', [])
            hits_set = set()
            # exact match by file_path/name if provided
            for s, chunk in results:
                matched = False
                for r in relevant:
                    rp = r.get('file_path')
                    if rp and chunk.file_path.endswith(rp) and chunk.chunk_type == r.get('chunk_type') and (not r.get('name') or r.get('name') in chunk.name):
                        matched = True
                        break
                if not matched:
                    for term in relevant_contains:
                        if term and (term in chunk.file_path or term in chunk.name or term in chunk.content):
                            matched = True
                            break
                if matched:
                    hits_set.add(chunk.chunk_id)
            hits = len(hits_set)
            # compute union of relevant identifiers to calculate recall correctly
            relevant_keys = []
            for r in relevant:
                key = f"{r.get('file_path','')}|{r.get('chunk_type','')}|{r.get('name','')}".strip()
                if key:
                    relevant_keys.append(key)
            for rc in relevant_contains:
                if rc:
                    relevant_keys.append(str(rc))
            relevant_total = len(set(relevant_keys)) if relevant_keys else 0
            recall = hits / relevant_total if relevant_total > 0 else 0.0
            if recall > 1.0:
                recall = 1.0
            precision = hits / max(qtop, 1)
            per_query.append({
                'query': query_text,
                'top_k': qtop,
                'hits': hits,
                'recall': recall,
                'precision': precision,
            })
            if hits > 0:
                hits_total += 1

        avg_recall = sum(p['recall'] for p in per_query) / max(total, 1)
        avg_precision = sum(p['precision'] for p in per_query) / max(total, 1)
        accuracy = hits_total / max(total, 1)
        # Restore original setting if modified
        if restored is not None:
            try:
                engine.config['idf_boost_enabled'] = restored
            except Exception:
                pass

        return {
            'total_queries': total,
            'accuracy': accuracy,
            'avg_precision': avg_precision,
            'avg_recall': avg_recall,
            'per_query': per_query,
        }


def _noop():
    pass
