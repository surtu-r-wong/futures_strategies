"""A pure-index replica of the CITIC Futures commodity strategy indices.

Separate from `cta_carry` on purpose.  CITIC publishes an unlevered index off a
2010-01-04 base of 1000; the production engine runs a vol-targeted, cost-bearing
book.  Asking "how close is the replica to the official series" is a question
the production engine cannot answer, so this package rebuilds the index the way
the methodology specifies and nothing else.

Every rule the shipped basis-momentum leg departs from is a switch here, so the
attribution task can price the deviations one at a time.  See
`docs/plans/2026-09-11-cicsf027-pure-index-replica-design.md`.
"""
