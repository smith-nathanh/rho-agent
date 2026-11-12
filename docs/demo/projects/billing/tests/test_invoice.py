from invoice import invoice_total, split_evenly


def test_total_includes_tax():
    assert invoice_total([("hosting", 100.00)]) == 108.25


def test_split_shares_add_up_to_total():
    shares = split_evenly(100.00, 3)
    assert round(sum(shares), 2) == 100.00
