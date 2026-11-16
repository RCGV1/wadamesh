// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <istream>
#include <string>
bool desktopUiCommand(const std::string &command, std::istream &input, const std::string &directory, std::string &error);
