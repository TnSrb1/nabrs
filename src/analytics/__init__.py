"""Analytics over tender_awards (VG-05 → VG-06).

Read-only aggregations of award data: the profile of the contracting authority (who and how much
it awards) and the profile of a competitor. No AI, no writes — plain SELECTs,
near-zero cost. See :mod:`src.analytics.awards_analytics`.
"""
