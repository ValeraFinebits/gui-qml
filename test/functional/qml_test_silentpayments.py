#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Send a silent payment through QML and verify its output and persisted metadata."""

from decimal import Decimal
import os
import re
import sys
import time
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bitcoin", "test", "functional"))

from qml_process_checks import report_gui_failure
from qml_wallet_test_lib import WalletFlowHarness, rpc_call, wait_for_loaded_wallets
from test_framework.bip352 import scan_tx
from test_framework.descriptors import descsum_create
from test_framework.key import ECKey
from test_framework.segwit_addr import Encoding, bech32_encode, convertbits
from test_framework.wallet_util import bytes_to_wif


WALLET_NAME = "silentpayments_sender"
WALLET_PASSWORD = "silent payments test password"
SEND_AMOUNT = "1.00000000"
SEND_SATOSHIS = 100000000
NOTE = "Rent · September"


def wait_until(predicate, *, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def amount_satoshis(text):
    normalized = re.sub(r"\s", "", text).replace("\u2212", "-")
    match = re.search(r"-?[0-9]+(?:[.,][0-9]+)*", normalized)
    assert match, f"No amount in {text!r}"
    number = match.group()
    if "sat" in text:
        return int(number.replace(",", "").replace(".", ""))
    # BTC amounts have a fixed fractional part. The final separator is the
    # decimal point; preceding separators belong to the grouped integer part.
    separator = max(number.rfind("."), number.rfind(","))
    if separator >= 0:
        number = re.sub(r"[.,]", "", number[:separator]) + "." + number[separator + 1:]
    return int(Decimal(number) * 100000000)


def assert_amount_parser():
    for text, expected in (
        ("1.00000000 BTC", 100000000),
        ("-1.00000000 BTC", -100000000),
        ("1,00000000 BTC", 100000000),
        ("-1,00000000 BTC", -100000000),
        ("-1,234.00000001 BTC", -123400000001),
        ("\u22121.234,00000001 BTC", -123400000001),
        ("100\u202f000\u202f000 sat", 100000000),
        ("-100,000,000 sat", -100000000),
        ("\u2212100.000.000 sat", -100000000),
    ):
        actual = amount_satoshis(text)
        assert actual == expected, (text, actual, expected)


class ScannerRpc:
    """The two node methods used by the independent BIP352 scanner."""

    def __init__(self, port):
        self.port = port

    def gettxout(self, txid, index, include_mempool):
        return rpc_call(self.port, "gettxout", [txid, index, include_mempool])

    def getrawtransaction(self, txid, verbose):
        return rpc_call(self.port, "getrawtransaction", [txid, verbose])


def make_recipient():
    scan_key, spend_key = ECKey(), ECKey()
    scan_key.set((352).to_bytes(32, "big"), compressed=True)
    spend_key.set((35302).to_bytes(32, "big"), compressed=True)
    payload = scan_key.get_pubkey().get_bytes() + spend_key.get_pubkey().get_bytes()
    address = bech32_encode(Encoding.BECH32M, "sprt", [0] + convertbits(list(payload), 8, 5))
    return address, scan_key, spend_key


def assert_sp_output(harness, tx_hex, scan_key, spend_key, amount=SEND_SATOSHIS):
    matches = scan_tx(ScannerRpc(harness.gui_rpc_port), tx_hex,
                      scan_key.get_bytes(), spend_key.get_pubkey().get_bytes())
    assert len(matches) == 1, matches
    decoded = rpc_call(harness.gui_rpc_port, "decoderawtransaction", [tx_hex])
    outputs = [output for output in decoded["vout"]
               if output["scriptPubKey"]["hex"] == "5120" + matches[0]["output"]]
    assert len(outputs) == 1, outputs
    assert int(Decimal(str(outputs[0]["value"])) * 100000000) == amount


def assert_sp_marker_and_gui_bump(harness, gui, txid):
    # This SP-specific error confirms the marker, including after wallet reload.
    rpc_call(harness.gui_rpc_port, "walletpassphrase", [WALLET_PASSWORD, 60], wallet=WALLET_NAME)
    try:
        try:
            rpc_call(harness.gui_rpc_port, "psbtbumpfee", [txid], wallet=WALLET_NAME)
        except RuntimeError as error:
            assert str(error) == "psbtbumpfee does not support silent payments transactions, use bumpfee instead"
        else:
            raise AssertionError("The sent transaction lost its silent payments marker")

        gui.wait_for_property("activityDetailsPage", "detailsLoading", False, timeout_ms=20000)
        gui.wait_for_property("activityDetailsPage", "statusKnown", True, timeout_ms=20000)
        assert gui.get_property("activityDetailsPage", "txid") == txid
        assert gui.get_property("activityDetailsPage", "depth") == 0
        assert gui.get_property("activityDetailsPage", "inactive") is False
        gui.wait_for_property("activityDetailsPage", "canBump", True, timeout_ms=10000)
        gui.wait_for_property("speedUpBanner", "visible", True, timeout_ms=10000)
    finally:
        rpc_call(harness.gui_rpc_port, "walletlock", wallet=WALLET_NAME)


def assert_payment_note(harness, gui, txid):
    gui.wait_for_property("activityDetailsPage", "txid", txid, timeout_ms=10000)
    gui.wait_for_property("activityDetailsPage", "detailsLoading", False, timeout_ms=20000)
    transaction = rpc_call(harness.gui_rpc_port, "gettransaction", [txid], wallet=WALLET_NAME)
    assert transaction["comment"] == NOTE, transaction
    recipient, = [entry for entry in transaction["details"] if entry["category"] == "send"]
    gui.wait_for_property(f"transactionFlowOutput_{recipient['vout']}", "title", NOTE, timeout_ms=20000)
    decoded = rpc_call(harness.gui_rpc_port, "decoderawtransaction", [transaction["hex"]])
    for output in decoded["vout"]:
        if output["n"] != recipient["vout"]:
            assert gui.get_property(f"transactionFlowOutput_{output['n']}", "title") != NOTE
    assert gui.get_property("transactionFlowInput_0", "title") != NOTE


def reload_wallet(harness, gui):
    rpc_call(harness.gui_rpc_port, "unloadwallet", [WALLET_NAME])
    wait_for_loaded_wallets(harness.gui_rpc_port, [])
    gui.wait_for_property("walletBadge", "noWalletLoaded", True, timeout_ms=20000)
    rpc_call(harness.gui_rpc_port, "loadwallet", [WALLET_NAME])
    wait_for_loaded_wallets(harness.gui_rpc_port, [WALLET_NAME])
    gui.wait_for_property("walletBadge", "text", WALLET_NAME, timeout_ms=20000)
    gui.wait_for_property("walletBadge", "noWalletLoaded", False, timeout_ms=20000)
    gui.wait_for_property("walletBadge", "loading", False, timeout_ms=20000)


def recipient_fields(gui):
    return [gui.get_text(name) for name in ("sendAddressInput", "sendAmountInput", "sendNoteInput")] + [
        gui.get_property(name, "value") for name in ("sendPaymentRequestPayTo", "sendPaymentRequestMessageText")
    ]


def bump_through_gui(harness, gui, txid, *, cancel_first=False, mine_to=None):
    port = harness.gui_rpc_port
    gui.wait_for_property("activityDetailsPage", "txid", txid, timeout_ms=10000)
    gui.wait_for_property("activityStack", "busy", False, timeout_ms=10000)
    gui.wait_for_property("activityDetailsPage", "canBump", True, timeout_ms=10000)
    gui.wait_for_property("speedUpBannerPrimaryButton", "visible", True, timeout_ms=10000)
    gui.wait_for_property("speedUpBannerPrimaryButton", "enabled", True, timeout_ms=10000)
    gui.click("speedUpBannerPrimaryButton")
    gui.wait_for_property("speedUpOverlay", "opened", True, timeout_ms=10000)
    gui.wait_for_property("speedUpPassphrasePopup", "opened", True, timeout_ms=10000)
    assert gui.get_property("updateTransactionButton", "enabled") is False
    if cancel_first:
        gui.click("speedUpPassphraseCancelButton")
        gui.wait_for_property("speedUpPassphrasePopup", "visible", False, timeout_ms=10000)
        gui.wait_for_property("speedUpOverlay", "visible", False, timeout_ms=10000)
        assert rpc_call(port, "getrawmempool") == [txid]
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        gui.click("speedUpBannerPrimaryButton")
        gui.wait_for_property("speedUpOverlay", "opened", True, timeout_ms=10000)
        gui.wait_for_property("speedUpPassphrasePopup", "opened", True, timeout_ms=10000)
        gui.set_text("speedUpPassphraseField", "incorrect password")
        gui.click("speedUpPassphraseConfirmButton")
        gui.wait_for_property("speedUpPassphraseErrorText", "text", "The wallet password you entered was incorrect.", timeout_ms=10000)
    gui.set_text("speedUpPassphraseField", WALLET_PASSWORD)
    gui.click("speedUpPassphraseConfirmButton")
    gui.wait_for_property("speedUpPassphrasePopup", "visible", False, timeout_ms=10000)
    gui.wait_for_property("updateTransactionButton", "enabled", True, timeout_ms=10000)
    old_fee = amount_satoshis(gui.get_property("speedUpOriginalFeeRow", "value"))
    new_fee = amount_satoshis(gui.get_property("speedUpNewFeeRow", "value"))
    assert new_fee > old_fee
    assert rpc_call(port, "getrawmempool") == [txid]
    assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
    if mine_to:
        rpc_call(port, "generatetoaddress", [1, mine_to])
    gui.click("updateTransactionButton")
    if mine_to:
        gui.wait_for_property("speedUpErrorText", "text", "Transaction can no longer be bumped.", timeout_ms=10000)
        assert rpc_call(port, "getrawmempool") == []
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        gui.click("speedUpCloseButton")
        gui.wait_for_property("speedUpOverlay", "visible", False, timeout_ms=10000)
        return None
    gui.wait_for_property("speedUpCompletePopup", "opened", True, timeout_ms=10000)
    bumped_txid = gui.get_property("speedUpCompletePopup", "txid")
    assert bumped_txid != txid
    wait_until(lambda: rpc_call(port, "getrawmempool") == [bumped_txid], description="GUI replacement in mempool")
    transaction = rpc_call(port, "gettransaction", [bumped_txid], wallet=WALLET_NAME)
    assert int(abs(Decimal(str(transaction["fee"]))) * 100000000) == new_fee
    assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
    gui.click("speedUpCompleteViewTransactionButton")
    gui.wait_for_property("activityDetailsPage", "txid", bumped_txid, timeout_ms=10000)
    return bumped_txid


def maximum_draft(gui, address, *, coin=None, fee_rate=None, amount=SEND_AMOUNT):
    gui.click("sendTabButton")
    gui.wait_for_page("sendPage", timeout_ms=10000)
    gui.invoke("sendPage", "resetSendForm")
    gui.set_text("sendAddressInput", address)
    gui.set_text("sendAmountInput", amount)
    gui.set_text("sendNoteField", NOTE)
    if coin is not None:
        gui.click("sendCoinControlPickerOption_1")
        gui.click("sendSelectInputsButton")
        gui.wait_for_property("coinSelectionPopup", "opened", True, timeout_ms=10000)
        gui.set_text("coinBrowserSearch", f"{coin['txid']}:{coin['vout']}")
        gui.wait_for_property("coinSelectionListView", "count", 1, timeout_ms=10000)
        gui.click_list_item("coinSelectionListView", 0, "coinSelectionCheckbox")
        gui.click("coinSelectionDoneButton")
        gui.wait_for_property("coinSelectionPopup", "visible", False, timeout_ms=10000)
    gui.click("feeSelectionPickerButton")
    gui.click("feeSelectionOption3" if fee_rate else "feeSelectionOption1")
    if fee_rate:
        gui.set_text("feeSelectionCustomRateInput", fee_rate)
    gui.click("sendUseMaximumButton")


def unlock_maximum_review(gui, *, sweep):
    gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
    gui.click("sendReviewButton")
    if sweep:
        gui.wait_for_property("sendReviewSweepAlert", "opened", True, timeout_ms=10000)
        gui.click("sendReviewSweepConfirmButton")
    gui.wait_for_property("reviewPassphrasePopup", "opened", True, timeout_ms=10000)
    assert gui.get_property("sendReviewSweepAlert", "visible") is False
    gui.set_text("reviewPassphraseField", WALLET_PASSWORD)
    gui.click("reviewPassphraseConfirmButton")


def test_use_maximum(harness, gui, mining_address, address, scan_key, spend_key):
    port = harness.gui_rpc_port
    rpc_call(port, "generatetoaddress", [2, mining_address])
    internal_key, leaf_key = ECKey(), ECKey()
    internal_key.set((35203).to_bytes(32, "big"), compressed=True)
    leaf_key.set((35204).to_bytes(32, "big"), compressed=True)
    leaf = bytes_to_wif(leaf_key.get_bytes())
    descriptors = [f"tr({internal_key.get_pubkey().get_bytes()[1:].hex()},pk({leaf}))", f"wsh(pk({leaf}))"]
    rpc_call(port, "walletpassphrase", [WALLET_PASSWORD, 60], wallet=WALLET_NAME)
    try:
        imports = rpc_call(port, "importdescriptors", [[
            {"desc": descsum_create(descriptor), "timestamp": "now"} for descriptor in descriptors
        ]], wallet=WALLET_NAME)
        assert all(result["success"] for result in imports), imports
        incompatible_addresses = [rpc_call(port, "deriveaddresses", [
            rpc_call(port, "getdescriptorinfo", [descriptor])["descriptor"]
        ])[0] for descriptor in descriptors]
        tiny_address = rpc_call(port, "getnewaddress", {"address_type": "bech32"}, wallet=WALLET_NAME)
        rpc_call(port, "sendmany", ["", {**dict.fromkeys(incompatible_addresses, 1), tiny_address: 0.00001}], wallet=WALLET_NAME)
    finally:
        rpc_call(port, "walletlock", wallet=WALLET_NAME)
    rpc_call(port, "generatetoaddress", [2, mining_address])
    coins = rpc_call(port, "listunspent", wallet=WALLET_NAME)
    incompatible = [next(coin for coin in coins if coin["address"] == destination) for destination in incompatible_addresses]
    assert all(coin["spendable"] and coin["solvable"] for coin in incompatible)
    tiny = next(coin for coin in coins if coin["address"] == tiny_address)
    good = next(coin for coin in coins if coin["address"] == mining_address)
    assert rpc_call(port, "getrawmempool") == []

    for coin, error in ((None, "Missing the private key of silent payments eligible input"),
                        (incompatible[0], "Missing the private key of silent payments eligible input"),
                        (incompatible[1], "No silent payment eligible inputs were found.")):
        maximum_draft(gui, address, coin=coin)
        gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        unlock_maximum_review(gui, sweep=coin is None)
        wait_until(lambda: error in gui.get_property("reviewPassphraseErrorText", "text"), description=error)
        assert gui.get_property("transactionReviewPopup", "visible") is False
        assert rpc_call(port, "getrawmempool") == []
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        gui.click("reviewPassphraseCancelButton")
        gui.wait_for_property("reviewPassphrasePopup", "visible", False, timeout_ms=10000)

    for fee_rate in ("6", "100"):
        maximum_draft(gui, address, coin=tiny, fee_rate=fee_rate, amount="0.00001000")
        gui.wait_for_property("sendNetworkFeeSection", "footerText",
                              "Unable to calculate the maximum amount.", timeout_ms=20000)
        assert gui.get_property("sendReviewButton", "enabled") is False
        assert rpc_call(port, "getrawmempool") == []
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0

    for manual in (True, False):
        reserved = None
        if not manual:
            rpc_call(port, "walletpassphrase", [WALLET_PASSWORD, 60], wallet=WALLET_NAME)
            try:
                rpc_call(port, "sendall", {"recipients": [mining_address], "fee_rate": 1, "options": {
                    "inputs": [{"txid": coin["txid"], "vout": coin["vout"]} for coin in incompatible],
                }}, wallet=WALLET_NAME)
            finally:
                rpc_call(port, "walletlock", wallet=WALLET_NAME)
            rpc_call(port, "generatetoaddress", [1, mining_address])
            reserved = next(coin for coin in rpc_call(port, "listunspent", wallet=WALLET_NAME) if coin["address"] == mining_address)
            reserved = {key: reserved[key] for key in ("txid", "vout", "amount")}
            rpc_call(port, "lockunspent", [False, [{"txid": reserved["txid"], "vout": reserved["vout"]}], True], wallet=WALLET_NAME)
        maximum_draft(gui, address, coin=good if manual else None, fee_rate="2" if manual else None)
        gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
        assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        assert gui.get_property("reviewPassphrasePopup", "visible") is False
        if manual:
            old_fee = amount_satoshis(gui.get_property("sendTotalFeesValue", "value"))
            gui.set_text("feeSelectionCustomRateInput", "4.2")
            gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
            assert amount_satoshis(gui.get_property("sendTotalFeesValue", "value")) > old_fee
        amount = amount_satoshis(gui.get_text("sendAmountInput") + " BTC")
        fee = amount_satoshis(gui.get_property("sendTotalFeesValue", "value"))
        available = int((Decimal(str(good["amount"])) if manual else
                         Decimal(str(rpc_call(port, "getbalance", wallet=WALLET_NAME))) - Decimal(str(reserved["amount"]))) * 100000000)
        assert amount + fee == available, (amount, fee, available)

        if manual:
            gui.click("sendReviewButton")
            gui.wait_for_property("reviewPassphrasePopup", "opened", True, timeout_ms=10000)
            gui.click("reviewPassphraseCancelButton")
            gui.wait_for_property("reviewPassphrasePopup", "visible", False, timeout_ms=10000)
            assert rpc_call(port, "getrawmempool") == []
            assert rpc_call(port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
            assert gui.get_property("sendUseMaximumButton", "enabled") is False
        unlock_maximum_review(gui, sweep=not manual)
        gui.wait_for_property("transactionReviewPopup", "opened", True, timeout_ms=20000)
        assert amount_satoshis(gui.get_property("sendTransactionReviewRecipient0Amount", "text")) == amount
        assert amount_satoshis(gui.get_property("sendTransactionReviewFee", "value")) == fee
        assert amount_satoshis(gui.get_property("sendTransactionReviewTotal", "value")) == available
        review_rate = Decimal(gui.get_property("sendTransactionReviewFeeRate", "value").split()[0])
        gui.click("sendTransactionReviewSendButton")
        if not manual:
            gui.wait_for_property("sendSweepAlert", "opened", True, timeout_ms=10000)
            gui.click("sendSweepConfirmButton")
        gui.wait_for_page("sendCompletePage", timeout_ms=20000)
        txid, = rpc_call(port, "getrawmempool")
        transaction = rpc_call(port, "gettransaction", [txid], wallet=WALLET_NAME)
        decoded = rpc_call(port, "decoderawtransaction", [transaction["hex"]])
        assert len(decoded["vout"]) == 1, decoded
        if manual:
            assert [(item["txid"], item["vout"]) for item in decoded["vin"]] == [(good["txid"], good["vout"])]
        assert int(abs(Decimal(str(transaction["fee"]))) * 100000000) == fee
        actual_rate = Decimal(fee) / decoded["vsize"]
        assert abs(review_rate - actual_rate) <= Decimal("0.001")
        requested_rate = Decimal("4.2" if manual else "1")
        assert requested_rate <= actual_rate <= requested_rate * (1 + Decimal(2 * len(decoded["vin"])) / decoded["vsize"])
        assert_sp_output(harness, transaction["hex"], scan_key, spend_key, amount)
        gui.click("sendResultViewTransactionButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_payment_note(harness, gui, txid)
        gui.click("activityDetailsBackButton")
        reload_wallet(harness, gui)
        gui.click("activityTabButton")
        gui.set_text("activitySearchField", txid)
        gui.wait_for_property("activityFilterProxyModel", "count", 1, timeout_ms=20000)
        gui.click_list_item("activityListView", 0, "activityRowOpenButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_payment_note(harness, gui, txid)
        gui.wait_for_property("transactionSilentPaymentAddress", "address", address, timeout_ms=10000)
        assert_sp_marker_and_gui_bump(harness, gui, txid)
        if not manual:
            rpc_call(port, "lockunspent", [True, [{"txid": reserved["txid"], "vout": reserved["vout"]}]], wallet=WALLET_NAME)
            replacement_txid = bump_through_gui(harness, gui, txid)
            replacement = rpc_call(port, "gettransaction", [replacement_txid], wallet=WALLET_NAME)
            replacement_decoded = rpc_call(port, "decoderawtransaction", [replacement["hex"]])
            assert len(replacement_decoded["vin"]) > len(decoded["vin"])
            assert_sp_output(harness, replacement["hex"], scan_key, spend_key, amount)
            gui.click("activityDetailsBackButton")
            reload_wallet(harness, gui)
            gui.click("activityTabButton")
            gui.set_text("activitySearchField", replacement_txid)
            gui.wait_for_property("activityFilterProxyModel", "count", 1, timeout_ms=20000)
            gui.click_list_item("activityListView", 0, "activityRowOpenButton")
            assert_payment_note(harness, gui, replacement_txid)
            gui.wait_for_property("transactionSilentPaymentAddress", "address", address, timeout_ms=10000)
            bump_through_gui(harness, gui, replacement_txid, mine_to=mining_address)
        gui.click("activityDetailsBackButton")
        rpc_call(port, "generatetoaddress", [1, mining_address])


def run_test():
    assert_amount_parser()
    harness = WalletFlowHarness("qml_test_silentpayments", port_offset=150)
    try:
        harness.start_gui()
        gui = harness.driver
        gui.wait_for_property("walletBadge", "loading", False, timeout_ms=20000)
        rpc_call(harness.gui_rpc_port, "createwallet", {
            "wallet_name": WALLET_NAME, "passphrase": WALLET_PASSWORD,
        })
        gui.wait_for_property("walletBadge", "text", WALLET_NAME, timeout_ms=20000)
        gui.wait_for_property("walletBadge", "noWalletLoaded", False, timeout_ms=20000)
        mining_address = rpc_call(harness.gui_rpc_port, "getnewaddress", {
            "address_type": "bech32",
        }, wallet=WALLET_NAME)
        rpc_call(harness.gui_rpc_port, "generatetoaddress", [101, mining_address])
        wait_until(lambda: rpc_call(harness.gui_rpc_port, "getbalance", wallet=WALLET_NAME) >= 50,
                   description="mature wallet balance")

        address, scan_key, spend_key = make_recipient()
        assert len(address) > 90
        gui.click("sendTabButton")
        gui.wait_for_page("sendPage", timeout_ms=10000)
        gui.set_text("sendAddressInput", address)
        gui.set_text("sendAmountInput", SEND_AMOUNT)
        gui.set_text("sendNoteField", NOTE)
        gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
        assert gui.get_text("sendAddressInput").replace(" ", "") == address
        assert gui.get_property("sendUseMaximumButton", "enabled") is True
        assert gui.get_property("feeSelectionEstimateLabel", "value") == "—"
        uri_message = "SP request · September"
        before_import = recipient_fields(gui)
        for index, (fallback, key) in enumerate((("", "sp"), (mining_address, "req-sp"))):
            uri = f"bitcoin:{fallback}?" + urlencode({
                key: address, "amount": SEND_AMOUNT, "label": "Alice", "message": uri_message,
            }, quote_via=quote)
            gui.set_clipboard_text(uri)
            gui.wait_for_property("clipboardUriPasteButton", "visible", True, timeout_ms=10000)
            gui.click("clipboardUriPasteButton")
            if index == 0:
                gui.wait_for_property("sendPaymentUriOverwritePopup", "opened", True, timeout_ms=10000)
                assert recipient_fields(gui) == before_import
                gui.click("sendPaymentUriOverwriteCancelButton")
                gui.wait_for_property("sendPaymentUriOverwritePopup", "visible", False, timeout_ms=10000)
                assert recipient_fields(gui) == before_import
                gui.click("clipboardUriPasteButton")
                gui.wait_for_property("sendPaymentUriOverwritePopup", "opened", True, timeout_ms=10000)
                gui.click("sendPaymentUriOverwriteConfirmButton")
                gui.wait_for_property("sendPaymentUriOverwritePopup", "visible", False, timeout_ms=10000)
            gui.wait_for_property("sendPaymentRequestMessageText", "value", uri_message, timeout_ms=10000)
            assert gui.get_text("sendAddressInput").replace(" ", "") == address
            assert amount_satoshis(gui.get_text("sendAmountInput") + " BTC") == SEND_SATOSHIS
            assert gui.get_property("sendPaymentRequestPayTo", "value") == "Alice"
            assert gui.get_text("sendNoteInput") == uri_message + " - Alice"
            assert gui.get_property("sendUseMaximumButton", "enabled") is True

        before_import = recipient_fields(gui)
        gui.invoke("walletSendPage", "openPaymentRequestImport")
        gui.wait_for_property("sendUriImportPopup", "opened", True, timeout_ms=10000)
        gui.set_text("sendUriImportInput", f"bitcoin:{mining_address}?sp=invalid&amount=2&label=Changed&message=Changed")
        gui.click("sendUriImportApplyButton")
        gui.wait_for_property("sendUriImportPopup", "visible", False, timeout_ms=10000)
        gui.wait_for_property("sendPaymentRequestStatusText", "text", "Not a valid silent payment address.", timeout_ms=10000)
        assert recipient_fields(gui) == before_import

        uri_path = os.path.join(harness.tmpdir, "silent-payment-uri.txt")
        with open(uri_path, "w", encoding="utf-8") as uri_file:
            uri_file.write(uri)
        gui.set_text("sendNoteField", "Changed before file import")
        gui.invoke("walletSendPage", "applyPaymentRequestFromFile", [uri_path])
        gui.wait_for_property("walletSendPage", "paymentRequestIsError", False, timeout_ms=10000)
        assert recipient_fields(gui) == before_import
        gui.set_text("sendNoteField", NOTE)
        assert gui.get_property("sendPaymentRequestMessageText", "value") == uri_message
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == []
        transaction_count = rpc_call(harness.gui_rpc_port, "getwalletinfo", wallet=WALLET_NAME)["txcount"]

        gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
        gui.click("sendReviewButton")
        gui.wait_for_property("reviewPassphrasePopup", "opened", True, timeout_ms=10000)
        gui.set_text("reviewPassphraseField", "incorrect password")
        gui.click("reviewPassphraseConfirmButton")
        gui.wait_for_property("reviewPassphraseErrorText", "text",
                              "The wallet password you entered was incorrect.", timeout_ms=10000)
        assert rpc_call(harness.gui_rpc_port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        gui.set_text("reviewPassphraseField", WALLET_PASSWORD)
        gui.click("reviewPassphraseConfirmButton")
        gui.wait_for_page("sendTransactionReviewPage", timeout_ms=20000)
        gui.wait_for_property("transactionReviewPopup", "opened", True, timeout_ms=10000)
        assert rpc_call(harness.gui_rpc_port, "getwalletinfo", wallet=WALLET_NAME)["unlocked_until"] == 0
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == []
        assert gui.get_property("sendTransactionReviewRecipient0", "description").replace(" ", "") == address
        assert gui.get_property("sendTransactionReviewRecipient0", "title") == NOTE
        gui.click("sendTransactionReviewCloseButton")
        gui.wait_for_property("transactionReviewPopup", "visible", False, timeout_ms=10000)
        gui.wait_for_page("sendPage", timeout_ms=10000)
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == []
        assert rpc_call(harness.gui_rpc_port, "getwalletinfo", wallet=WALLET_NAME)["txcount"] == transaction_count
        gui.click("sendReviewButton")
        gui.wait_for_property("reviewPassphrasePopup", "opened", True, timeout_ms=10000)
        gui.set_text("reviewPassphraseField", WALLET_PASSWORD)
        gui.click("reviewPassphraseConfirmButton")
        gui.wait_for_property("transactionReviewPopup", "opened", True, timeout_ms=20000)
        # Changing the draft must not change the reviewed payment's note.
        gui.set_text("sendNoteField", "Unreviewed note")
        assert gui.get_text("sendNoteInput") == "Unreviewed note"
        assert gui.get_property("sendTransactionReviewRecipient0", "title") == NOTE
        review_amount = gui.get_property("sendTransactionReviewRecipient0Amount", "text")
        assert amount_satoshis(review_amount) == SEND_SATOSHIS, repr(review_amount)
        review_fee = amount_satoshis(gui.get_property("sendTransactionReviewFee", "value"))
        assert review_fee > 0
        assert amount_satoshis(gui.get_property("sendTransactionReviewTotal", "value")) == SEND_SATOSHIS + review_fee
        psbt_path = os.path.join(harness.tmpdir, "silent-payment.psbt")
        gui.invoke("sendTransactionReviewPage", "savePsbt", [psbt_path])
        gui.wait_for_property("sendTransactionReviewPage", "saveError", True, timeout_ms=10000)
        assert "silent payments" in gui.get_property("sendTransactionReviewPage", "saveStatus").lower()
        assert not os.path.exists(psbt_path)

        gui.click("sendTransactionReviewSendButton")
        gui.wait_for_page("sendCompletePage", timeout_ms=20000)
        wait_until(lambda: len(rpc_call(harness.gui_rpc_port, "getrawmempool")) == 1,
                   description="one broadcast transaction")
        txid, = rpc_call(harness.gui_rpc_port, "getrawmempool")
        transaction = rpc_call(harness.gui_rpc_port, "gettransaction", [txid], wallet=WALLET_NAME)
        assert transaction["comment"] == NOTE, transaction
        assert int(abs(Decimal(str(transaction["fee"]))) * 100000000) == review_fee
        assert_sp_output(harness, transaction["hex"], scan_key, spend_key)

        gui.click("sendResultViewTransactionButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_payment_note(harness, gui, txid)
        assert_sp_marker_and_gui_bump(harness, gui, txid)
        gui.click("activityDetailsBackButton")
        reload_wallet(harness, gui)
        gui.click("activityTabButton")
        gui.wait_for_property("activitySearchField", "visible", True, timeout_ms=10000)
        gui.click("activityTypeFilterButton")
        gui.click("activityTypeSent")
        gui.invoke("activityTypeFilterPopup", "close")
        gui.wait_for_property("activityFilterProxyModel", "count", 1, timeout_ms=20000)
        for search in (address, NOTE):
            gui.set_text("activitySearchField", "unmatched-query")
            gui.wait_for_property("activityFilterProxyModel", "count", 0, timeout_ms=10000)
            gui.set_text("activitySearchField", search)
            gui.wait_for_property("activityFilterProxyModel", "count", 1, timeout_ms=20000)
            gui.wait_for_property(f"activityItem_{txid}", "label", NOTE, timeout_ms=10000)
        gui.wait_for_property(f"activityItem_{txid}", "txid", txid, timeout_ms=10000)
        gui.click_list_item("activityListView", 0, "activityRowOpenButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_payment_note(harness, gui, txid)
        assert_sp_marker_and_gui_bump(harness, gui, txid)
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == [txid]

        bumped_txid = bump_through_gui(harness, gui, txid, cancel_first=True)
        assert bumped_txid != txid
        wait_until(lambda: rpc_call(harness.gui_rpc_port, "getrawmempool") == [bumped_txid],
                   description="silent payment replacement in mempool")
        replacement = rpc_call(harness.gui_rpc_port, "gettransaction", [bumped_txid], wallet=WALLET_NAME)
        assert replacement["comment"] == NOTE, replacement
        assert int(abs(Decimal(str(replacement["fee"]))) * 100000000) > review_fee
        assert_sp_output(harness, replacement["hex"], scan_key, spend_key)
        gui.click("activityDetailsBackButton")
        reload_wallet(harness, gui)
        gui.click("activityTabButton")
        gui.set_text("activitySearchField", NOTE)
        gui.wait_for_property("activityFilterProxyModel", "count", 2, timeout_ms=20000)
        for row_index, current_txid in enumerate((bumped_txid, txid)):
            gui.wait_for_property(f"activityItem_{current_txid}", "label", NOTE, timeout_ms=10000)
            gui.click_list_item("activityListView", row_index, "activityRowOpenButton")
            gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
            assert_payment_note(harness, gui, current_txid)
            gui.wait_for_property("activityDetailsPage", "canBump", current_txid == bumped_txid, timeout_ms=10000)
            gui.click("activityDetailsBackButton")
        test_use_maximum(harness, gui, mining_address, address, scan_key, spend_key)
        print("Silent payment URI import, maximum sends, input errors, review fee, persisted notes, recipient data and GUI RBF verified")
    except BaseException:
        report_gui_failure(harness.gui_process, "silent payment send and wallet reload")
        raise
    finally:
        harness.stop()


if __name__ == "__main__":
    run_test()
