// Copyright (c) 2026 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import org.bitcoincore.qt 1.0

import "../../controls"
import "../../components"

Popup {
    id: root
    objectName: "speedUpOverlay"

    property string txid: ""
    property var bumpModel: null
    property var operationModel: null
    property string operationTxid: ""
    property int bumpState: root.operationModel ? root.operationModel.state : BumpTransactionModel.Idle
    property string bumpErrorText: root.operationModel ? root.operationModel.errorText : ""
    property bool readyToConfirm: root.isCurrentOperation()
        && root.bumpState === BumpTransactionModel.NeedsConfirmation

    function isCurrentOperation() {
        return root.opened && root.operationModel !== null
            && root.operationModel === root.bumpModel && root.operationTxid === root.txid
            && root.operationModel.oldTxid === root.operationTxid
    }

    function requestPassword() {
        speedUpPassphrasePopup.preparing = root.bumpState === BumpTransactionModel.Preparing
        speedUpPassphrasePopup.busy = false
        speedUpPassphrasePopup.errorText = ""
        speedUpPassphrasePopup.open()
    }

    onTxidChanged: if (visible && operationTxid !== txid) close()
    onBumpModelChanged: if (visible && operationModel !== bumpModel) close()

    signal done()
    signal viewNewTransaction(string newTxid)

    readonly property color modalOverlayColor: Qt.rgba(0, 0, 0, 0.4)
    property real verticalOffset: 0
    parent: Overlay.overlay
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) + verticalOffset : verticalOffset
    width: Math.min(640, parent ? parent.width - 40 : 640)
    modal: true
    focus: true
    leftPadding: width < 480 ? 20 : 40
    rightPadding: leftPadding
    topPadding: 30
    bottomPadding: 30
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    enter: Transition {
        NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 300; easing.type: Easing.OutCubic }
        NumberAnimation { property: "verticalOffset"; from: -30; to: 0; duration: 300; easing.type: Easing.OutCubic }
    }
    exit: Transition {
        NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 250; easing.type: Easing.InCubic }
        NumberAnimation { property: "verticalOffset"; from: 0; to: -30; duration: 250; easing.type: Easing.InCubic }
    }
    Overlay.modal: Rectangle {
        color: root.modalOverlayColor
        opacity: root.opacity
    }

    onOpened: {
        root.operationModel = root.bumpModel
        root.operationTxid = root.txid
        if (root.bumpModel) {
            root.bumpModel.prepareFeeBump(root.txid, 1)
            if (root.bumpModel.needsUnlock) root.requestPassword()
        }
    }

    onAboutToHide: {
        const model = root.operationModel
        root.operationModel = null
        speedUpPassphrasePopup.close()
        if (model && model.oldTxid === root.operationTxid) model.reset()
    }

    background: Rectangle {
        color: Theme.color.neutral1
        radius: 10
        border.color: Theme.color.neutral3
        border.width: 1
        SurfaceGradientBorder {
            anchors.fill: parent
            surfaceColor: parent.color
            cornerRadius: parent.radius
        }
    }

    property string newTxid: ""

    signal bumpSucceeded()

    Connections {
        target: root.operationModel
        function onStateChanged() {
            if (root.opened && root.operationModel && root.operationModel.state !== BumpTransactionModel.Idle
                    && root.operationModel.oldTxid !== root.operationTxid) {
                root.close()
                return
            }
            if (root.isCurrentOperation() && root.operationModel.state === BumpTransactionModel.Succeeded) {
                root.newTxid = root.operationModel.newTxid
                root.close()
                root.bumpSucceeded()
            }
        }
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Header {
                Layout.fillWidth: true
                header: qsTr("Speed up transaction")
                headerBold: true
                center: false
            }
            CloseButton {
                objectName: "speedUpCloseButton"
                onClicked: root.close()
            }
        }

        CoreText {
            Layout.fillWidth: true
            Layout.topMargin: 15
            Layout.bottomMargin: 25
            horizontalAlignment: Text.AlignLeft
            text: qsTr("Set a higher transaction fee if you want your transaction to be confirmed faster.")
            font.pixelSize: 15
            color: Theme.color.neutral7
            wrap: true
        }

        FormSection {
            objectName: "speedUpFeeSection"
            visible: root.readyToConfirm
            Layout.fillWidth: true
            backgroundColor: Theme.color.neutral2
            ValueRow {
                objectName: "speedUpOriginalFeeRow"
                amountValue: true
                Layout.fillWidth: true
                title: qsTr("Original fee")
                value: root.operationModel ? root.operationModel.oldFee : ""
                valueTextStyle: Theme.text.monoCaption
                valueMaximumWidth: root.availableWidth * 0.6
                dividerColor: Theme.color.neutral3
            }
            ValueRow {
                objectName: "speedUpNewFeeRow"
                amountValue: true
                Layout.fillWidth: true
                title: qsTr("New fee")
                value: root.operationModel ? root.operationModel.newFee : ""
                valueTextStyle: Theme.text.monoCaption
                valueMaximumWidth: root.availableWidth * 0.6
                showDivider: false
            }
        }

        CoreText {
            objectName: "speedUpErrorText"
            visible: root.bumpState === BumpTransactionModel.Failed
            text: root.bumpErrorText
            font.pixelSize: 15
            color: Theme.color.red
            Layout.fillWidth: true
            Layout.topMargin: 10
            wrap: true
        }

        GridLayout {
            Layout.fillWidth: true
            Layout.topMargin: 24
            columns: root.width < 480 ? 1 : 2
            columnSpacing: 12
            rowSpacing: 12

            OutlineButton {
                text: qsTr("Cancel")
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                onClicked: root.close()
            }

            ContinueButton {
                objectName: "updateTransactionButton"
                text: qsTr("Update transaction")
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                enabled: root.readyToConfirm
                onClicked: {
                    if (!root.isCurrentOperation()) {
                        return
                    }
                    const model = root.operationModel
                    if (!model.confirmFeeBump() && root.isCurrentOperation() && model.needsUnlock) root.requestPassword()
                }
            }
        }
    }

    WalletPassphrasePopup {
        id: speedUpPassphrasePopup
        property bool preparing: false
        parent: Overlay.overlay
        width: Math.min(420, root.width - 40)
        popupObjectName: "speedUpPassphrasePopup"
        passphraseFieldObjectName: "speedUpPassphraseField"
        errorTextObjectName: "speedUpPassphraseErrorText"
        cancelButtonObjectName: "speedUpPassphraseCancelButton"
        confirmButtonObjectName: "speedUpPassphraseConfirmButton"
        titleText: qsTr("Enter wallet password")
        descriptionText: preparing
            //: Password prompt to prepare and sign a replacement transaction for fee review. Nothing is sent yet.
            ? qsTr("Enter your wallet password to prepare this transaction for review.")
            : qsTr("Enter your wallet password to update this transaction.")
        confirmText: preparing
            //: Unlock the wallet to prepare the replacement. Sending requires a later confirmation.
            ? qsTr("Unlock and continue") : qsTr("Unlock and update")
        busyConfirmText: qsTr("Unlocking...")
        onClosed: if (preparing && root.bumpState === BumpTransactionModel.Preparing) root.close()
        onSubmitted: (passphrase) => {
            if (!root.isCurrentOperation()) {
                return
            }
            speedUpPassphrasePopup.busy = true
            const model = root.operationModel
            const succeeded = preparing
                ? model.prepareFeeBumpWithPassphrase(root.operationTxid, 1, passphrase)
                : model.confirmFeeBumpWithPassphrase(passphrase)
            if (succeeded) {
                speedUpPassphrasePopup.busy = false
                speedUpPassphrasePopup.close()
                return
            }
            speedUpPassphrasePopup.busy = false
            if (root.isCurrentOperation()) {
                if (model.needsUnlock) {
                    speedUpPassphrasePopup.errorText = model.errorText
                } else {
                    speedUpPassphrasePopup.close()
                }
            }
        }
    }

}
