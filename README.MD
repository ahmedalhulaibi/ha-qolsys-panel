# HA Qolsys Panel

[![Build](https://github.com/ahmedalhulaibi/ha-qolsys-panel/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmedalhulaibi/ha-qolsys-panel/actions/workflows/ci.yml)

This is a HACS custom integration for **Qolsys Panels** (such as IQ Panel 2+/4), allowing local pairing and control via Home Assistant.

This fork of [EHylands/ha-qolsys-panel](https://github.com/EHylands/ha-qolsys-panel)
adds Argon2id PIN verification and respects Home Assistant's selected network
adapters when advertising the pairing server. The integration domain and existing
pairing certificates are unchanged.

## 🧩 Installation

To use this fork with HACS, add `https://github.com/ahmedalhulaibi/ha-qolsys-panel`
as a custom **Integration** repository. Use this repository instead of installing
the upstream repository alongside it; both provide the `qolsys_panel` domain.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ahmedalhulaibi&repository=ha-qolsys-panel&category=Integration+)

For a manual installation, copy `custom_components/qolsys_panel` from a pinned
commit of this fork into your Home Assistant configuration directory, then restart
Home Assistant. Keep `<config>/qolsys_panel` intact: it contains pairing credentials
and the PIN verifier file. This fork pins `qolsys-controller==1.12.7` and
`cryptography==50.0.2`; it is tested with Home Assistant 2026.11.0.

## ⚙️ Pairing Configuration

You can pair with your Qolsys Panel using one of the following two methods:

### 🔍 1. Panel Autodiscovery and Pairing

**Requirements:**

- Home Assistant server and Qolsys Panel must be on the **same network subnet** for automatic mDNS pairing to work.
- You must have the **installer code** for your Qolsys Panel.

**Steps:**

1. On your Qolsys Panel, access the IQ Remote pairing page:  
   `Settings → Advanced Settings → Installation → Devices → WI-FI Devices → IQ Remote Devices`

2. In Home Assistant, go to:  
   `Settings → Devices & Services → Integrations → Add Integration`

3. Search for **Qolsys Panel**.

4. When prompted for the pairing method, choose:  **Automatic Panel Discovery and Pairing**

### 🔐 2. Pairing with an Existing Signed Client Certificate (PKI)

If you already have a **signed client certificate**, you can use it to securely pair with your Qolsys panel.  

With this method, **Home Assistant and the Qolsys Panel do not need to be on the same subnet**, since it doesn’t rely on mDNS discovery.

**Requirements:**

- Qolsys Panel IP address
- A valid PKI folder located at:  
  `/homeassistant/qolsys_panel/pki/[***random_mac***]/`

  Example contents of the PKI folder (`f2163e3a5fb1` is the random_mac):

  ```
  /homeassistant/qolsys_panel/pki/f2163e3a5fb1/
    ├── f2163e3a5fb1.key
    ├── f2163e3a5fb1.cer
    ├── f2163e3a5fb1.csr
    ├── f2163e3a5fb1.qolsys
    ├── f2163e3a5fb1.secure
  ```

**Steps:**

1. **In Home Assistant**:
   - Navigate to:  
`Settings → Devices & Services → Integrations → Add Integration`

2. Search for **Qolsys Panel**.

3. When prompted for the pairing method, choose: **Use Existing PKI**

4. Enter your **Qolsys Panel IP**

5. Select the appropriate PKI folder from the dropdown menu (it will list folders from `/homeassistant/qolsys_panel/pki/`)

## 🛠️ PKI Auto-Discovery and Pairing Troubleshooting

If pairing is not working, please verify the following:

1. **Same subnet**: Ensure the Home Assistant server and Qolsys Panel are on the same subnet.

2. **mDNS filtering**: Check your network equipment for mDNS filtering or storm-control features. Some Wi-Fi systems and managed switches may block or limit mDNS traffic, which can prevent device discovery.

3. **Pairing service advertisement**: Verify that the pairing service is being advertised. While the integration is in pairing mode, use a discovery tool (for example, the **Discovery** app on iPhone) to confirm that the `.http NsdPairService` service is visible on the network.

4. **Key-exchange server interface**: Review the Home Assistant logs when starting pairing. Confirm that the key-exchange server is listening on the expected IP address and network interface. If the wrong interface is selected, you can configure the default network adapter in:
   `Settings → System → Network → Network Adapter`

## 🎛️ Integration Configuration Options

After pairing, you can adjust the integration's behavior at any time from:
`Settings → Devices & Services → Qolsys Panel → Configure`

| Option | Description | Default |
| --- | --- | --- |
| **User Code required to Arm** | Require a user code to arm the panel. | `off` |
| **User Code required to Disarm** | Require a user code to disarm the panel. | `on` |
| **Enable Trigger Police Alarm** | Expose the panic/police alarm trigger. | `off` |
| **Enable Trigger Auxiliary Alarm** | Expose the auxiliary (medical) alarm trigger. | `off` |
| **Enable Trigger Fire Alarm** | Expose the fire alarm trigger. | `off` |
| **Enable Peek In Picture** | Enable retrieval of Peek-In pictures from supported cameras. | `on` |
| **Enable Motion Sensor Delay** | Apply a delay before a motion sensor returns to its cleared state. | `off` |
| **Motion Sensor Delay (seconds)** | Delay, in seconds, applied when the motion sensor delay is enabled. | `310` |

> Changing an option reloads the integration so the new settings take effect immediately.

### 🔑 User Codes (`users.conf`)

When a code is required, this fork verifies a separate **Home Assistant PIN**
locally, then sends the associated panel user ID. It does not change the physical
panel's user code. A missing, malformed, or plaintext PIN file fails closed.

After the integration connects, open **Settings → Devices & Services → Qolsys
Panel → Configure → Set or change Home Assistant PIN**. Choose your panel user,
enter a separate 4–12 digit PIN twice in the masked fields, and submit. PINs are
never prefilled or returned in the form, saved in config entry options, or logged
by the integration. A PIN update is available immediately without a restart.

An interactive terminal helper is also available. For a Home Assistant Container
installation named `homeassistant`, run on its host:

```sh
docker exec -it homeassistant python /config/custom_components/qolsys_panel/set_pin.py
```

Choose your existing panel user ID from the displayed list and enter a separate
4–12 digit PIN twice. Input is hidden and cannot be supplied on the command line.
No alarm command is issued by the helper. Add `--check` to verify a PIN locally
without operating the panel.

The helper writes `<config>/qolsys_panel/users.conf` atomically with mode `0600`.
Each record contains an `id` and `user_code_hash`, using Argon2id with 64 MiB of
memory, three passes, four lanes, a unique 32-byte random salt, and a 32-byte output.
PIN updates take effect immediately. Verification runs outside Home Assistant's
event loop, with one KDF allocation at a time and a limit of five failed attempts
per 60 seconds per integration instance.

Plaintext `user_code` records are deliberately rejected. If moving from an
upstream installation, securely remove the old plaintext file and any plaintext
copies you created, then use the helper to enroll a new Home Assistant PIN. The
fork never converts or backs up plaintext PIN records. Four-digit PINs are
supported; longer PINs remain supported for existing credentials. Short PINs are
easier to guess even with a salted hash. The online failed-attempt limit remains
enabled for every supported PIN length.

The salt is stored with the hash and is not a secret. Hashing protects the stored
PIN verifier; an administrator with access to Home Assistant or its pairing
credentials can still control the panel.

### Private pairing credentials and encrypted storage

This fork enforces `0700` on the Qolsys data directories and `0600` on their
files, including private keys and signed certificates. Setup and pairing fail
closed on symlinks, hard-linked files, unexpected ownership, or a permissions
error. The SDK creates new credentials inside an already private ancestor, and
the fork restricts the resulting files after generation and pairing.

Permissions protect access by other local users. To encrypt credentials at rest,
place `<config>/qolsys_panel` on an encrypted filesystem. Its unlock passphrase
should be managed on the host, outside the integration and its config entry.

Deployments that require manual unlock can create a root-owned `0600` guard at
`<config>/.qolsys-storage.json`, with `version: 1`, `require_mount: true`, and a
`volume_id` containing 32 lowercase hexadecimal characters. Store the same ID in
`<config>/qolsys_panel/.qolsys-volume-id` with mode `0600` on the encrypted volume.
The integration requires a mounted filesystem and matching marker before any
credential setup. This prevents a locked or missing volume from silently falling
back to plaintext storage. Home Assistant can run its other integrations while
Qolsys waits for the volume to be unlocked. On Docker installations, restart the
Home Assistant container after mounting the volume so it sees the new submount.

## ✅ Supported Devices

Any **Qolsys Panel** that can be paired with an **IQ Remote**:

- IQ Panel 2+
- IQ Panel 4
- IQ 4 Hub
- IQ 4 NS (IQ Installer app)
- IQ Hub
- IQ Panel 5
- IQ Pro (IQ Installer app)

## 📦 Supported Functionality

The integration emulates an IQ Remote and exposes the panel's capabilities to Home Assistant through the entities and actions below, grouped by what each represents on the panel.

### Partitions

- **Alarm Control Panel** (`alarm_control_panel`): one per partition; reports the arming status and alarm state, and supports arming (home, away, night) and disarming.
- **Binary sensors** (`binary_sensor`): Exit Sounds, Entry Delays and Quick Exit status, plus dedicated Fire, Police, CO and Auxiliary alarm-active sensors.
- **Switches** (`switch`): toggle Exit Sounds and Entry Delay, and set the Arm Stay Instant (Arm Home with no exit delay) and Arm Stay Silent Disarming modifiers for the partition.
- **Sensor** (`sensor`): the last error reported by the partition.

### Panel

- **Diagnostic binary sensors** (`binary_sensor`): AC status, tamper, battery status, communication failure, Z-Wave controller, secure arming, auto stay, auto arm stay, auto bypass and Control4.
- **Doorbell** and **Chime** (`binary_sensor`): pulse when the panel reports a doorbell press or a chime.
- **Media Player** (`media_player`): text-to-speech; use the standard speak/`tts` actions to make the panel talk.
- **Scenes** (`scene`): activate scenes defined on the panel.
- **Weather** (`weather`): the panel's weather forecast.
- **Peek In Picture** (`image`): the most recent Peek In snapshot; refreshed by the `update_picture_peek_in` action.

### Zones (security sensors)

- **Binary sensor** (`binary_sensor`): open/closed (or motion, etc.) status for each zone, plus per-zone Unreachable, Tamper, Battery and AC-status sensors.
- **Sensors** (`sensor`): Latest and Average signal strength (dBm) and Battery Level/Voltage, plus Temperature and Light on supported PowerG devices.

### Automation devices (Z-Wave, PowerG, Zigbee, Alarm.com)

Depending on the device type, the integration creates the matching platform entity:

- **Lock** (`lock`): door locks.
- **Light** (`light`) and **Switch** (`switch`): dimmers and outlets.
- **Climate** (`climate`): Z-Wave and Alarm.com thermostats. Alarm.com thermostats report the current temperature and humidity, and support Off, Heat, Cool and Heat/Cool modes, heat and cool setpoints, and fan modes (On, Auto).
- **Cover** (`cover`) and **Valve** (`valve`): controllable openings.
- **Siren** (`siren`).
- **Sensors** (`sensor`): battery level, generic sensor readings and energy-meter values.
- **Binary sensor** (`binary_sensor`): device reachability/status.
- **Event** (`event`): Z-Wave **Central Scene** buttons (scene controllers, wall remotes, etc.). The integration creates one `button`-class event entity per supported scene (button) on each Central Scene endpoint — named `Scene <n>` (with `Endpoint <e>` appended for multi-endpoint devices). These entities are stateless: each key press reported by the panel fires the matching event type (`single_tap`, `double_tap`, `hold`, `release`, …, as advertised by the device), letting you trigger Home Assistant automations directly from button presses.

### Actions

Most actions target a partition's `alarm_control_panel` entity; `change_master_volume` and `update_picture_peek_in` target the Qolsys Panel itself.

| Action | Target | Description | Options |
|--------|--------|-------------|---------|
| `qolsys_panel.trigger_police` | Partition | Trigger a Police alarm on the panel | `silent` (boolean) |
| `qolsys_panel.trigger_auxilliary` | Partition | Trigger an Auxiliary alarm on the panel | `silent` (boolean) |
| `qolsys_panel.trigger_fire` | Partition | Trigger a Fire alarm on the panel | — |
| `qolsys_panel.quick_exit` | Partition | Open a door while Armed-Stay without triggering an alarm | `duration` (seconds) |
| `qolsys_panel.change_master_volume` | Panel | Change the panel's master volume | `volume` (0–15) |
| `qolsys_panel.update_picture_peek_in` | Panel | Capture a Peek In snapshot and update the panel's Peek In Picture (`image`) entity | — |

## 🔄 Data Updates

This integration is **local push**. It maintains a persistent local MQTT connection to the panel (by emulating an IQ Remote) and receives state changes in real time as the panel reports them within moments of a change. The integration does **not** poll the panel on an interval. If the connection drops it reconnects automatically.

## ⚠️ Known Limitations

- **IQ Remote emulation** — The integration communicates with the panel by emulating an IQ Remote device. Only features and devices the panel exposes to IQ Remotes can be monitored or controlled

## 🔧 Removing the Integration

This integration follows the standard Home Assistant integration removal process — **no extra steps are required**.

To remove an integration instance from Home Assistant:

1. Go to **Settings** > **Devices & Services**.
2. Find and select the **integration card** you want to remove.
3. From the list of devices, locate the specific **integration instance**.
4. Click the **three dots menu** (⋮) next to the entry.
5. Select **Delete**.

The integration will be cleanly removed from your system.

### 🧹 Removing Stale Devices

When a zone, automation device or camera is removed from the Qolsys Panel, its device in Home Assistant is cleaned up **automatically** the next time the integration reloads (or Home Assistant restarts).

You can also remove a stale device **manually** at any time:

1. Go to **Settings** > **Devices & Services** > **Qolsys Panel**.
2. Open the device you want to remove.
3. Click the **three dots menu** (⋮) and select **Delete**.

Only devices that the panel no longer reports are removed. Devices still present on the panel — and the main **Panel** device itself — are kept; to remove those, delete the integration instead.

## ✨ Functionality Highlights

| Category               | Feature                              | Status |
|------------------------|--------------------------------------|--------|
| **Panel**              | Diagnostic Sensors                   | ✅     |
|                        | Panel Scenes                         | ✅     |
|                        | Speak Command                        | ✅     |
|                        | Weather Forecast                     | ✅     |
| **Partition**          | Arming Status and Alarm State        | ✅     |
|                        | Home Instant Arming                  | ✅     |
|                        | Home Silent Disarming (Firmware 4.6.1)| ✅     |
|                        | Trigger Alarm                         | ✅     |
|                        | Enable or Disable User Code           | ✅     |
|                        | Set Exit Sounds and Entry Delay       | ✅     |
| **Zones**              | Sensor Status                        | ✅     |
|                        | Tamper State                         | ✅     |
|                        | Battery Level                        | ✅     |
|                        | Temperature (supported PowerG device)| ✅     |
|                        | Light (supported PowerG device)      | ✅     |
|                        | Average and Latest dBm               | ✅     |

| Automation Devices| Z-Wave | PowerG | Zigbee | Alarm.com |
|-----------------|--------|--------|-------|-------|
| Door Lock        | ✅     | ✅     | ✅    | ❌    |
| Energy Clamp     | ✅     | ❌     | ❌    | ❌    |
| External Siren   | ✅     | ❌     | ❌    | ❌    |
| Garage Door      | ✅     | ❌     | ❌    | ✅    |
| Light            | ✅     | ✅     | ✅    | ✅    |
| Scene Controller (Central Scene) | ✅     | ❌     | ❌    | ❌    |
| Smart Outlet     | 🛠️     | ❌     | ❌    | ❌    |
| Thermometer      | ✅     | ❌     | ❌    | ❌    |
| Thermostat       | ✅     | ❌     | ❌    | ✅    |
| Water Valve      | 🛠️     | ❌     | ❌    | ❌    |

🛠️ = partially supported or untested
