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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bitcoin", "test", "functional"))

from qml_process_checks import report_gui_failure
from qml_wallet_test_lib import WalletFlowHarness, rpc_call, wait_for_loaded_wallets
from test_framework.bip352 import scan_tx
from test_framework.key import ECKey
from test_framework.segwit_addr import Encoding, bech32_encode, convertbits


WALLET_NAME = "silentpayments_sender"
WALLET_PASSWORD = "silent payments test password"
SEND_AMOUNT = "1.00000000"
SEND_SATOSHIS = 100000000


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


def assert_sp_output(harness, tx_hex, scan_key, spend_key):
    matches = scan_tx(ScannerRpc(harness.gui_rpc_port), tx_hex,
                      scan_key.get_bytes(), spend_key.get_pubkey().get_bytes())
    assert len(matches) == 1, matches
    decoded = rpc_call(harness.gui_rpc_port, "decoderawtransaction", [tx_hex])
    outputs = [output for output in decoded["vout"]
               if output["scriptPubKey"]["hex"] == "5120" + matches[0]["output"]]
    assert len(outputs) == 1, outputs
    assert int(Decimal(str(outputs[0]["value"])) * 100000000) == SEND_SATOSHIS


def assert_sp_marker_and_no_gui_bump(harness, gui, txid):
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
        assert gui.get_property("activityDetailsPage", "canBump") is False
        assert gui.get_property("speedUpBanner", "visible") is False
    finally:
        rpc_call(harness.gui_rpc_port, "walletlock", wallet=WALLET_NAME)


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

        # TODO: Cover SP sends without change, with explicitly selected inputs and with a custom fee.
        # Compare the requested rate (allowing signed-size variation), Review fee/rate and actual transaction.
        address, scan_key, spend_key = make_recipient()
        assert len(address) > 90
        # TODO: Cover BIP321 SP URI import with and without a fallback address.
        gui.click("sendTabButton")
        gui.wait_for_page("sendPage", timeout_ms=10000)
        gui.set_text("sendAddressInput", address)
        gui.set_text("sendAmountInput", SEND_AMOUNT)
        gui.wait_for_property("sendReviewButton", "enabled", True, timeout_ms=20000)
        assert gui.get_text("sendAddressInput").replace(" ", "") == address
        assert gui.get_property("sendUseMaximumButton", "enabled") is False
        assert gui.get_property("feeSelectionEstimateLabel", "value") == "—"
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == []

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
        assert int(abs(Decimal(str(transaction["fee"]))) * 100000000) == review_fee
        assert_sp_output(harness, transaction["hex"], scan_key, spend_key)

        gui.click("sendResultViewTransactionButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_sp_marker_and_no_gui_bump(harness, gui, txid)
        gui.click("activityDetailsBackButton")
        rpc_call(harness.gui_rpc_port, "unloadwallet", [WALLET_NAME])
        wait_for_loaded_wallets(harness.gui_rpc_port, [])
        gui.wait_for_property("walletBadge", "noWalletLoaded", True, timeout_ms=20000)
        rpc_call(harness.gui_rpc_port, "loadwallet", [WALLET_NAME])
        wait_for_loaded_wallets(harness.gui_rpc_port, [WALLET_NAME])
        gui.wait_for_property("walletBadge", "text", WALLET_NAME, timeout_ms=20000)
        gui.wait_for_property("walletBadge", "noWalletLoaded", False, timeout_ms=20000)
        gui.wait_for_property("walletBadge", "loading", False, timeout_ms=20000)
        gui.click("activityTabButton")
        # TODO: Search history by the original SP address after reloading the wallet.
        gui.wait_for_property("activitySearchField", "visible", True, timeout_ms=10000)
        gui.click("activityTypeFilterButton")
        gui.click("activityTypeSent")
        gui.invoke("activityTypeFilterPopup", "close")
        gui.wait_for_property("activityFilterProxyModel", "count", 1, timeout_ms=20000)
        gui.wait_for_property(f"activityItem_{txid}", "txid", txid, timeout_ms=10000)
        gui.click_list_item("activityListView", 0, "activityRowOpenButton")
        gui.wait_for_page("activityDetailsPage", timeout_ms=10000)
        assert_sp_marker_and_no_gui_bump(harness, gui, txid)
        assert rpc_call(harness.gui_rpc_port, "getrawmempool") == [txid]

        # Bumping without replacement outputs requires the SP recipients saved before reload.
        rpc_call(harness.gui_rpc_port, "walletpassphrase", [WALLET_PASSWORD, 60], wallet=WALLET_NAME)
        try:
            bumped = rpc_call(harness.gui_rpc_port, "bumpfee", [txid, {"fee_rate": 10}], wallet=WALLET_NAME)
        finally:
            rpc_call(harness.gui_rpc_port, "walletlock", wallet=WALLET_NAME)
        bumped_txid = bumped["txid"]
        assert bumped_txid != txid
        wait_until(lambda: rpc_call(harness.gui_rpc_port, "getrawmempool") == [bumped_txid],
                   description="silent payment replacement in mempool")
        replacement = rpc_call(harness.gui_rpc_port, "gettransaction", [bumped_txid], wallet=WALLET_NAME)
        assert int(abs(Decimal(str(replacement["fee"]))) * 100000000) > review_fee
        assert_sp_output(harness, replacement["hex"], scan_key, spend_key)
        print("Silent payment output, review fee, reloaded recipient data and disabled GUI RBF verified")
    except BaseException:
        report_gui_failure(harness.gui_process, "silent payment send and wallet reload")
        raise
    finally:
        harness.stop()


if __name__ == "__main__":
    run_test()
