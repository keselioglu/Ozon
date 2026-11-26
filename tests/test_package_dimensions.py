"""Package weight/size per product type (Ozon rejects outliers with ML_INCORRECT_VOLUME_WEIGHT)."""
from upload_to_ozon import SINGLE_ITEM_DIMS, package_dimensions


def test_underwear_and_socks_unchanged():
    assert package_dimensions("Brazilian Külot") == SINGLE_ITEM_DIMS
    assert package_dimensions("Pamuklu Çorap") == SINGLE_ITEM_DIMS
    assert package_dimensions("3'lü Hipster Külot")["weight"] == 100 + 2 * 80


def test_garments_get_their_own_size():
    jacket = package_dimensions("Stormwear Bomber Ceket")
    assert jacket["weight"] >= 800 and jacket["depth"] >= 35
    assert package_dimensions("Midi Elbise")["weight"] == 400
    assert package_dimensions("Saten Slip Etek")["weight"] == 300   # a skirt, not underwear
    assert package_dimensions("3'lü T-Shirt Seti")["weight"] == 600  # packs scale by the item weight
    assert package_dimensions(None) == SINGLE_ITEM_DIMS
