import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Omarchy Sync: a bar icon plus a compact panel that publishes this machine's
// Omarchy config to a private git repo and applies the repo back. The backend
// is sync.py; this file only renders state and calls actions.
Panel {
  id: root
  moduleName: "io.github.kaplanhalil.sync"
  ipcTarget: "io.github.kaplanhalil.sync"
  // Own IpcHandler so the settings view can be summoned directly.
  manageIpc: false

  property var status: ({})
  property string phase: ""
  property string notice: ""
  property string noticeDetail: ""
  property string errorText: ""
  property string confirming: ""
  property string view: "main"
  property int footerTick: 0

  readonly property color foreground: bar ? bar.barForeground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.35)
  readonly property color accentColor: Commons.Color.accent
  readonly property color urgentColor: Commons.Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool busy: phase !== ""
  readonly property bool linked: status.linked === true
  readonly property int localCount: Model.length(status.local_changes)
  readonly property int remoteCount: Model.length(status.remote_changes)
  readonly property int refreshMinutes: Model.refreshMinutes(status)
  readonly property string state: Model.stateOf(status, busy)
  readonly property color stateColor: {
    if (state === "local" || state === "diverged" || state === "error") return urgentColor
    if (state === "remote") return accentColor
    if (state === "unlinked" || state === "busy") return dim
    return foreground
  }

  readonly property string scriptPath: String(Qt.resolvedUrl("sync.py")).replace(/^file:\/\//, "")

  readonly property bool verticalBar: bar ? bar.vertical : false
  implicitWidth: verticalBar ? (bar ? bar.barSize : Style.bar.sizeVertical) : Style.bar.iconSlot
  implicitHeight: verticalBar ? Style.bar.iconSlot : (bar ? bar.barSize : Style.bar.sizeHorizontal)

  function run(action, args) {
    if (proc.running) return
    phase = action
    errorText = ""
    notice = ""
    noticeDetail = ""
    confirming = ""
    proc.handled = false
    proc.action = action
    proc.command = ["python3", scriptPath, action].concat(args || [])
    proc.running = true
  }

  function refresh(fetch) {
    run("status", fetch ? ["--fetch"] : [])
  }

  function connectRepo(target) {
    var value = String(target || "").trim()
    if (value === "") {
      errorText = "Repo address is empty"
      return
    }
    run("link", [value])
  }

  function publish() {
    if (busy) return
    run("publish", [])
  }

  function applyNow() {
    if (busy) return
    if (confirming !== "apply") {
      confirming = "apply"
      confirmTimer.restart()
      return
    }
    run("apply", [])
  }

  function undoNow() {
    if (busy) return
    if ((status.backups || 0) === 0) return
    if (confirming !== "undo") {
      confirming = "undo"
      confirmTimer.restart()
      return
    }
    run("undo", [])
  }

  function openSettings() {
    view = "settings"
    errorText = ""
    notice = ""
    noticeDetail = ""
    confirming = ""
  }

  function closeSettings() {
    view = "main"
    errorText = ""
    notice = ""
    noticeDetail = ""
  }

  // Optimistically flip the switch so the knob moves on the click, then let the
  // backend response replace the whole status object.
  function applyCategoryUpdate(key, value) {
    var base = status || ({})
    var settings = base.settings || ({})
    var source = settings.categories || ({})
    var categories = {}
    for (var name in source) categories[name] = source[name]

    if (key === "all") {
      var rows = Model.settingsRows()
      for (var i = 0; i < rows.length; i++) categories[rows[i].key] = value
    } else {
      categories[key] = value
    }

    var nextSettings = {}
    for (var field in settings) nextSettings[field] = settings[field]
    nextSettings.categories = categories

    var next = {}
    for (var prop in base) next[prop] = base[prop]
    next.settings = nextSettings
    status = next
  }

  function setCategory(key, value) {
    if (busy) return
    applyCategoryUpdate(key, value)
    run("settings", [key, value ? "true" : "false"])
  }

  function setAll(value) {
    if (busy) return
    applyCategoryUpdate("all", value)
    run("settings", ["all", value ? "true" : "false"])
  }

  function resetCategories() {
    if (busy) return
    run("settings", ["reset"])
  }

  function setRefresh(minutes) {
    if (busy) return
    var base = status || ({})
    var settings = base.settings || ({})
    var nextSettings = {}
    for (var field in settings) nextSettings[field] = settings[field]
    nextSettings.refresh_minutes = minutes
    var next = {}
    for (var prop in base) next[prop] = base[prop]
    next.settings = nextSettings
    status = next
    run("settings", ["refresh", String(minutes)])
  }

  function finish(action, raw, stderrText) {
    phase = ""
    var data = null
    try {
      data = JSON.parse(String(raw || "").trim())
    } catch (error) {
      data = null
    }
    if (!data) {
      errorText = String(stderrText || "").trim() || "sync.py returned no response"
      noticeTimer.stop()
      return
    }
    if (data.status) status = data.status
    if (data.ok === false) {
      errorText = data.error || "Operation failed"
      noticeTimer.stop()
      return
    }
    errorText = ""
    if (action === "publish") {
      notice = data.changed > 0 ? "Published " + data.changed + " files" : "No changes"
      noticeDetail = data.pushed === false ? "Commit is local; push failed" : "just now"
    } else if (action === "apply") {
      notice = data.applied > 0 ? "Applied " + data.applied + " files" : "Already up to date"
      noticeDetail = data.backup ? "backup saved" : "just now"
      if (Array.isArray(data.plugins) && data.plugins.length > 0) {
        notice += " · " + data.plugins.length + " plugins installed"
      }
    } else if (action === "undo") {
      notice = "Restored " + (data.restored || 0) + " files"
      noticeDetail = "backup restored"
    } else if (action === "link") {
      notice = "Repo linked"
      noticeDetail = Model.repoLine(data.status)
    } else if (action === "settings") {
      notice = "Settings saved"
      noticeDetail = ""
    } else {
      notice = ""
      noticeDetail = ""
    }
    if (notice !== "") noticeTimer.restart()
  }

  onOpenedChanged: {
    if (opened) {
      view = "main"
      refresh(true)
    }
  }

  Timer {
    id: confirmTimer
    interval: 6000
    onTriggered: root.confirming = ""
  }

  Timer {
    id: noticeTimer
    interval: 6000
    onTriggered: {
      root.notice = ""
      root.noticeDetail = ""
    }
  }

  Timer {
    interval: 60000
    running: root.opened
    repeat: true
    onTriggered: root.footerTick++
  }

  // Fetch shortly after login and then at the configured interval. The panel
  // also refreshes when opened, so a stale badge never survives a click.
  Timer {
    interval: Math.max(1, root.refreshMinutes) * 60000
    running: root.refreshMinutes > 0
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!root.busy) root.refresh(true)
  }

  Process {
    id: proc
    property string action: ""
    property bool handled: false

    stdout: StdioCollector {
      id: stdoutCollector
      waitForEnd: true
      onStreamFinished: {
        if (proc.handled) return
        proc.handled = true
        root.finish(proc.action, String(text || ""), String(stderrCollector.text || ""))
      }
    }

    stderr: StdioCollector {
      id: stderrCollector
      waitForEnd: true
    }

    onExited: function(exitCode) {
      if (proc.handled) return
      proc.handled = true
      root.finish(proc.action, String(stdoutCollector.text || ""), String(stderrCollector.text || ""))
    }
  }

  IpcHandler {
    target: "io.github.kaplanhalil.sync"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function showSettings(): void {
      root.open()
      root.openSettings()
    }

    function setSyncCategory(key: string, enabled: string): void {
      root.setCategory(String(key), String(enabled) === "true")
    }

    function setSyncRefresh(minutes: string): void {
      root.setRefresh(parseInt(String(minutes)))
    }

    function runPublish(): void { root.publish() }
    function runApply(): void { root.run("apply", []) }
    function runUndo(): void { root.run("undo", []) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: Model.icon(root.state)
    slotSize: Style.bar.iconSlot
    fontSize: Style.bar.iconFont
    foreground: root.stateColor
    tooltipText: Model.tooltip(root.state)
    onPressed: function(button) {
      if (button === Qt.RightButton) root.refresh(true)
      else root.toggle()
    }

    NumberAnimation on textRotation {
      id: barSpin
      running: root.busy
      loops: Animation.Infinite
      from: 0
      to: 360
      duration: 900
      onRunningChanged: if (!running) button.textRotation = 0
    }
  }

  // State dot rides on top of the stable cloud glyph. Plain items do not take
  // pointer input, so the bar click target is untouched.
  Rectangle {
    visible: Model.dotVisible(root.state)
    width: Style.space(5)
    height: width
    radius: width / 2
    color: root.state === "remote" ? root.accentColor : root.urgentColor
    anchors.top: parent.top
    anchors.right: parent.right
    anchors.topMargin: Style.space(3)
    anchors.rightMargin: Style.space(3)
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      spacing: Style.space(12)

      // ---------- Hero ----------
      PanelHero {
        width: parent.width
        title: "Omarchy Sync"
        meta: Model.statusTitle(root.state, root.localCount, root.remoteCount)
        detail: Model.statusChip(root.localCount, root.remoteCount)
        foreground: root.foreground
        fontFamily: root.fontFamily
        iconSize: Style.font.display

        iconComponent: Component {
          Text {
            text: Model.icon(root.state)
            color: root.stateColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.display

            RotationAnimation on rotation {
              running: root.busy
              loops: Animation.Infinite
              from: 0
              to: 360
              duration: 900
            }
          }
        }

        trailingControl: Component {
          Button {
            iconText: root.view === "settings" ? "\uf060" : "\uf013"
            bordered: false
            foreground: root.dim
            accent: root.accentColor
            horizontalPadding: Style.space(6)
            verticalPadding: Style.space(6)
            tooltipText: root.view === "settings" ? "Back" : "Sync settings"
            onClicked: root.view === "settings" ? root.closeSettings() : root.openSettings()
          }
        }
      }

      // ---------- Busy indicator ----------
      Item {
        width: parent.width
        height: Style.space(2)
        visible: root.busy
        clip: true

        Rectangle {
          anchors.fill: parent
          color: Qt.rgba(root.accentColor.r, root.accentColor.g, root.accentColor.b, 0.18)
        }

        Rectangle {
          id: progressBit
          width: parent.width * 0.32
          height: parent.height
          radius: height / 2
          color: root.accentColor

          SequentialAnimation on x {
            running: root.busy
            loops: Animation.Infinite
            NumberAnimation {
              from: -progressBit.width
              to: parent.width
              duration: 1000
              easing.type: Easing.InOutQuad
            }
          }
        }
      }

      // ---------- Result banner ----------
      BorderSurface {
        id: banner
        width: parent.width
        visible: opacity > 0
        opacity: (root.errorText !== "" || root.notice !== "") ? 1 : 0
        implicitHeight: bannerRow.implicitHeight + Style.space(18)
        radius: Style.cornerRadius
        color: root.errorText !== ""
          ? Qt.rgba(root.urgentColor.r, root.urgentColor.g, root.urgentColor.b, 0.10)
          : Qt.rgba(root.accentColor.r, root.accentColor.g, root.accentColor.b, 0.10)
        borderSpec: Border.flat(root.errorText !== "" ? root.urgentColor : root.accentColor, 1)

        Behavior on opacity { NumberAnimation { duration: 160 } }

        Row {
          id: bannerRow
          anchors.fill: parent
          anchors.margins: Style.space(9)
          spacing: Style.space(9)

          Text {
            id: bannerIcon
            text: root.errorText !== "" ? "\uf071" : "\uf058"
            color: root.errorText !== "" ? root.urgentColor : root.accentColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            width: parent.width - bannerIcon.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              text: root.errorText !== "" ? root.errorText : root.notice
              color: root.errorText !== "" ? root.urgentColor : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.Wrap
            }

            Text {
              width: parent.width
              visible: root.errorText === "" && root.noticeDetail !== ""
              text: root.noticeDetail
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }
      }

      // ---------- Main view ----------
      Column {
        id: mainView
        width: parent.width
        visible: root.view === "main"
        spacing: Style.space(12)

        // Repo chip
        BorderSurface {
          width: parent.width
          visible: root.linked
          implicitHeight: repoContent.implicitHeight + Style.space(14)
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.foreground, root.accentColor)
          borderSpec: Border.controlSpec("normal", root.foreground, root.accentColor)

          Row {
            id: repoContent
            anchors.left: parent.left
            anchors.leftMargin: Style.space(9)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(9)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            Text {
              text: "\uf126"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              text: Model.repoLine(root.status)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideMiddle
              width: parent.width - Style.space(20)
              anchors.verticalCenter: parent.verticalCenter
            }
          }
        }

        // Link flow
        Column {
          width: parent.width
          visible: !root.linked
          spacing: Style.space(8)

          Text {
            width: parent.width
            text: "Publish your config to a private git repo, then apply it on another machine. Link the repo first."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          TextField {
            id: linkField
            width: parent.width
            placeholderText: "https://github.com/user/omarchy-sync.git"
            foreground: root.foreground
            accent: root.accentColor
            onAccepted: root.connectRepo(text)
          }

          Button {
            width: parent.width
            text: root.phase === "link" ? "Connecting…" : "Connect"
            bordered: true
            foreground: root.foreground
            accent: root.accentColor
            onClicked: root.connectRepo(linkField.text)
          }
        }

        // Actions
        Row {
          width: parent.width
          visible: root.linked
          spacing: Style.space(8)

          Button {
            width: (parent.width - parent.spacing) / 2
            iconText: "\uf0ee"
            text: root.localCount > 0 ? "Publish · " + root.localCount : "Publish"
            bordered: true
            selected: root.localCount > 0
            verticalPadding: Style.space(8)
            foreground: root.foreground
            accent: root.accentColor
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.publish()
          }

          Button {
            width: (parent.width - parent.spacing) / 2
            iconText: "\uf0ed"
            text: root.confirming === "apply"
              ? "Confirm apply"
              : (root.remoteCount > 0 ? "Apply · " + root.remoteCount : "Apply")
            bordered: true
            selected: root.remoteCount > 0 || root.confirming === "apply"
            verticalPadding: Style.space(8)
            foreground: root.foreground
            accent: root.accentColor
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.applyNow()
          }
        }

        // Changes to publish
        Column {
          width: parent.width
          visible: root.linked && root.localCount > 0
          spacing: Style.space(4)

          PanelSectionHeader {
            text: "TO PUBLISH · " + root.localCount
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: Model.localRows(root.status, 4)

            delegate: Item {
              required property var modelData

              width: mainView.width
              implicitHeight: Style.space(22)

              Rectangle {
                width: Style.space(2)
                height: parent.height - Style.space(6)
                radius: width / 2
                color: root.urgentColor
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: localRowIcon
                text: modelData.icon
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                width: Style.space(20)
                anchors.left: parent.left
                anchors.leftMargin: Style.space(7)
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                text: modelData.path
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideMiddle
                anchors.left: localRowIcon.right
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }
          }

          Text {
            width: parent.width
            visible: Model.moreCount(root.status.local_changes, 4) > 0
            text: "+" + Model.moreCount(root.status.local_changes, 4) + " more"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.italic: true
          }
        }

        // Changes to apply
        Column {
          width: parent.width
          visible: root.linked && root.remoteCount > 0
          spacing: Style.space(4)

          PanelSectionHeader {
            text: "TO APPLY · " + root.remoteCount
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: Model.remoteRows(root.status, 4)

            delegate: Item {
              required property var modelData

              width: mainView.width
              implicitHeight: Style.space(22)

              Rectangle {
                width: Style.space(2)
                height: parent.height - Style.space(6)
                radius: width / 2
                color: root.accentColor
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: remoteRowIcon
                text: modelData.icon
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                width: Style.space(20)
                anchors.left: parent.left
                anchors.leftMargin: Style.space(7)
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                text: modelData.path
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideMiddle
                anchors.left: remoteRowIcon.right
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }
          }

          Text {
            width: parent.width
            visible: Model.moreCount(root.status.remote_changes, 4) > 0
            text: "+" + Model.moreCount(root.status.remote_changes, 4) + " more"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.italic: true
          }
        }

        // Footer
        PanelSeparator {
          visible: root.linked
          foreground: root.foreground
        }

        Item {
          width: parent.width
          visible: root.linked
          implicitHeight: Math.max(footerText.implicitHeight, undoButton.implicitHeight, refreshButton.implicitHeight)

          Text {
            id: footerText
            text: Model.footer(root.status, root.footerTick)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - undoButton.width - refreshButton.width - Style.space(16)
          }

          PanelActionButton {
            id: refreshButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: "\uf021"
            tooltipText: "Refresh"
            size: Style.space(26)
            foreground: root.foreground
            onClicked: root.refresh(true)
          }

          PanelActionButton {
            id: undoButton
            anchors.right: refreshButton.left
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.confirming === "undo" ? "\uf00c" : "\uf0e2"
            tooltipText: root.confirming === "undo" ? "Confirm undo" : "Undo"
            size: Style.space(26)
            foreground: root.foreground
            enabled: (root.status.backups || 0) > 0
            onClicked: root.undoNow()
          }
        }
      }

      // ---------- Settings view ----------
      Column {
        id: settingsView
        width: parent.width
        visible: root.view === "settings"
        spacing: Style.space(10)

        Text {
          width: parent.width
          text: "Pick the parts of your config this machine publishes and applies. Switched-off categories are ignored in both directions and never deleted from the repo."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        PanelSeparator { foreground: root.foreground }

        PanelSectionHeader {
          text: "GENERAL"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Column {
          width: parent.width
          spacing: Style.space(6)

          Text {
            width: parent.width
            text: "Auto-fetch"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Text {
            width: parent.width
            text: "How often the bar checks the remote for new commits."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          ButtonGroup {
            width: parent.width
            options: Model.refreshOptions()
            value: String(root.refreshMinutes)
            foreground: root.foreground
            accent: root.accentColor
            fontFamily: root.fontFamily
            onChanged: function(value) { root.setRefresh(parseInt(value)) }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Repeater {
          model: Model.settingsRows()

          delegate: Column {
            required property var modelData

            width: settingsView.width
            spacing: Style.space(4)

            PanelSectionHeader {
              visible: modelData.header !== ""
              text: modelData.header
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: parent.width
              implicitHeight: Style.space(34)

              Column {
                anchors.left: parent.left
                anchors.right: categorySwitch.left
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                spacing: 0

                Text {
                  width: parent.width
                  text: modelData.label
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  text: modelData.detail
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }

              ToggleSwitch {
                id: categorySwitch
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                checked: Model.categoryEnabled(root.status, modelData.key)
                busy: root.busy
                foreground: root.foreground
                accent: root.accentColor
                onToggled: root.setCategory(modelData.key, !checked)
              }
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Row {
          width: parent.width
          spacing: Style.space(8)

          Button {
            width: (parent.width - parent.spacing * 2) / 3
            text: "All"
            bordered: true
            foreground: root.foreground
            accent: root.accentColor
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.setAll(true)
          }

          Button {
            width: (parent.width - parent.spacing * 2) / 3
            text: "None"
            bordered: true
            foreground: root.foreground
            accent: root.accentColor
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.setAll(false)
          }

          Button {
            width: (parent.width - parent.spacing * 2) / 3
            text: "Reset"
            bordered: true
            foreground: root.foreground
            accent: root.accentColor
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.resetCategories()
          }
        }

        PanelSeparator { foreground: root.foreground }

        Button {
          width: parent.width
          iconText: "\uf0f4"
          text: "Buy me a coffee"
          bordered: true
          foreground: root.foreground
          accent: root.accentColor
          onClicked: Util.execArgv(["omarchy-launch-browser", Model.coffeeUrl()])
        }
      }
    }
  }
}
