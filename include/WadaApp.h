// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
void wadaStart();
void wadaSignalFirmwareReady();

// Validate the private queued channel intent against current native firmware settings.
bool wadaValidateChannelIntent(uint32_t packetId, uint8_t slot);
