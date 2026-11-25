#!/bin/bash

# ==============================
#  ADB Install / Uninstall Script
#  Path: /Volumes/Data/AndroidSDK/platform-tools/adb
#  Tự động kết nối IP: 192.168.1.17
#  Hỗ trợ: APK đơn, nhiều APK, XAPK/APKS/APKM (Split APKs + OBB)
# ==============================

# Ghi đè bằng biến môi trường; mặc định: adb trong PATH, rồi ANDROID_HOME, rồi đường dẫn máy cũ.
ADB="${ADB:-$(command -v adb 2>/dev/null || true)}"
[ -z "$ADB" ] && [ -n "${ANDROID_HOME:-}" ] && ADB="$ANDROID_HOME/platform-tools/adb"
[ -z "$ADB" ] && ADB="/Volumes/Data/AndroidSDK/platform-tools/adb"
TARGET_IP="${TARGET_IP:-192.168.1.17}"
TARGET_PORT="${TARGET_PORT:-5555}"
TARGET="$TARGET_IP:$TARGET_PORT"

# Các ABI token hợp lệ trong tên split (dùng dấu _ thay cho -)
KNOWN_ABIS="arm64_v8a armeabi_v7a armeabi x86_64 x86 mips64 mips"

# 1 = tự gỡ bản cũ khi xung đột chữ ký, không hỏi lại
AUTO_UNINSTALL=0

# Kiểm tra ADB
if [ ! -x "$ADB" ]; then
    echo "❌ Không tìm thấy ADB tại: $ADB"
    exit 1
fi

# aapt để đọc package name từ file APK (không bắt buộc)
AAPT=$(ls -d "$(dirname "$ADB")"/../build-tools/*/aapt 2>/dev/null | sort -V | tail -1)
[ -x "$AAPT" ] || AAPT=$(command -v aapt 2>/dev/null)

# Chạy lệnh với giới hạn thời gian: $1 = số giây, còn lại là lệnh.
# Khi xe tắt/ngoài vùng phủ, "adb connect" có thể treo rất lâu.
with_timeout() {
    local secs="$1"; shift
    local tmp; tmp=$(mktemp)
    "$@" > "$tmp" 2>&1 &
    local pid=$! i=0
    while [ $i -lt "$secs" ]; do
        kill -0 $pid 2>/dev/null || break
        sleep 1
        i=$((i + 1))
    done
    if kill -0 $pid 2>/dev/null; then
        kill -9 $pid 2>/dev/null
        wait $pid 2>/dev/null
        rm -f "$tmp"
        return 124
    fi
    wait $pid 2>/dev/null
    cat "$tmp"
    rm -f "$tmp"
    return 0
}

# Hàm kết nối và chọn thiết bị
connect_device() {
    echo "🔌 Đang kết nối tới $TARGET ..."
    local out
    out=$(with_timeout 12 "$ADB" connect "$TARGET")
    if [ $? -eq 124 ]; then
        echo "   ⚠️  Quá 12s không phản hồi — xe có thể đang tắt hoặc khác mạng WiFi."
    else
        case "$out" in
            *"connected to"*) ;;
            *) echo "   ⚠️  $out" ;;
        esac
    fi
    sleep 1

    local device_list
    device_list=$("$ADB" devices | awk '$2=="device" {print $1}')

    if [ -z "$device_list" ]; then
        echo "❌ Không tìm thấy thiết bị nào ở trạng thái 'device'."
        echo "👉 Trạng thái hiện tại:"
        "$ADB" devices | sed '1d;/^$/d' | sed 's/^/   /'
        echo "👉 Kiểm tra lại IP hoặc WiFi của xe."
        DEVICE=""
        return 1
    fi

    local picked
    picked=$(echo "$device_list" | grep -F "$TARGET_IP" | head -n 1)

    if [ -z "$picked" ]; then
        local count
        count=$(echo "$device_list" | wc -l | tr -d ' ')

        if [ "$count" -eq 1 ]; then
            picked=$(echo "$device_list" | head -n 1)
            echo "✅ Đã kết nối 1 thiết bị: $picked"
        else
            echo "⚠️  Phát hiện nhiều thiết bị:"
            echo "$device_list" | nl
            echo ""
            read -r -p "Chọn số thứ tự thiết bị muốn dùng: " NUM
            picked=$(echo "$device_list" | sed -n "${NUM}p")

            if [ -z "$picked" ]; then
                echo "❌ Lựa chọn không hợp lệ."
                DEVICE=""
                return 1
            fi
            echo "✅ Đã chọn thiết bị: $picked"
        fi
    else
        echo "✅ Đã kết nối xe: $picked"
    fi

    DEVICE="$picked"
    DEVICE_ABILIST=$("$ADB" -s "$DEVICE" shell getprop ro.product.cpu.abilist </dev/null 2>/dev/null | tr -d '\r')
    if [ -z "$DEVICE_ABILIST" ]; then
        DEVICE_ABILIST=$("$ADB" -s "$DEVICE" shell getprop ro.product.cpu.abi </dev/null 2>/dev/null | tr -d '\r')
    fi
    local sdk
    sdk=$("$ADB" -s "$DEVICE" shell getprop ro.build.version.sdk </dev/null 2>/dev/null | tr -d '\r')
    echo "   ℹ️  SDK: ${sdk:-?}   ABI: ${DEVICE_ABILIST:-?}"
    return 0
}

# Kiểm tra thiết bị còn sống trước mỗi thao tác
ensure_device() {
    if [ -z "$DEVICE" ]; then
        echo "❌ Chưa có thiết bị. Dùng chức năng 4 để kết nối lại."
        return 1
    fi
    local state
    state=$("$ADB" -s "$DEVICE" get-state </dev/null 2>&1 | tr -d '\r')
    if [ "$state" != "device" ]; then
        echo "❌ Thiết bị $DEVICE không sẵn sàng (trạng thái: $state)."
        echo "👉 Dùng chức năng 4 để kết nối lại."
        return 1
    fi
    return 0
}

# adb dùng \r cho thanh tiến trình -> phải tách dòng và bỏ dòng tiến trình,
# nếu không thông báo lỗi in ra sẽ bị cắt vụn.
extract_error() {
    local cleaned key
    cleaned=$(echo "$1" | tr '\r' '\n' | grep -v '^[[:space:]]*$' \
              | grep -viE 'Performing .*Install|^\[ *[0-9]+%\]|[0-9.]+ MB/s')
    key=$(echo "$cleaned" | grep -iE 'Failure|Error|Exception|^adb:' | tail -n 3)
    if [ -n "$key" ]; then
        echo "$key"
    else
        echo "$cleaned" | tail -n 3
    fi
}

# Chạy adb install và in lỗi thật khi thất bại.
# Tự thử lại không có -g (một số ROM không hỗ trợ grant-all-permissions).
run_install() {
    # adb quét đường dẫn .apk từ CUỐI argv, nên cờ phải đứng trước file.
    local cmd="$1"; shift
    local out rc
    out=$("$ADB" -s "$DEVICE" "$cmd" -r -g "$@" 2>&1)
    rc=$?
    if [ $rc -eq 0 ] && ! echo "$out" | grep -q "Failure\|Error\|Exception"; then
        return 0
    fi

    # Thử lại không có -g
    local out2 rc2
    out2=$("$ADB" -s "$DEVICE" "$cmd" -r "$@" 2>&1)
    rc2=$?
    if [ $rc2 -eq 0 ] && ! echo "$out2" | grep -q "Failure\|Error\|Exception"; then
        echo "   ⚠️  Đã cài được nhưng phải bỏ -g (không cấp sẵn quyền)."
        return 0
    fi

    LAST_INSTALL_ERROR=$(extract_error "$out2")
    [ -z "$LAST_INSTALL_ERROR" ] && LAST_INSTALL_ERROR=$(extract_error "$out")

    # Hạ cấp phiên bản: thử -d trước vì cách này GIỮ được dữ liệu app
    case "$LAST_INSTALL_ERROR" in
        *VERSION_DOWNGRADE*)
            local out3
            out3=$("$ADB" -s "$DEVICE" "$cmd" -r -d "$@" 2>&1)
            if [ $? -eq 0 ] && ! echo "$out3" | grep -q "Failure\|Error\|Exception"; then
                echo "   ⚠️  Đã cài đè bản cũ hơn (-d), dữ liệu app được giữ lại."
                return 0
            fi
            ;;
    esac
    return 1
}

# Lấy package name từ file APK bằng aapt
get_package_name() {
    [ -n "$AAPT" ] || return 1
    "$AAPT" dump badging "$1" 2>/dev/null \
        | sed -n "s/^package: name='\([^']*\)'.*/\1/p" | head -n 1
}

# Lấy package name từ thông báo lỗi: "Existing package com.waze signatures ..."
pkg_from_error() {
    echo "$1" | sed -n 's/.*Existing package \([A-Za-z0-9_.]*\).*/\1/p' | head -n 1
}

# Lỗi có phải do app đã cài sẵn và xung đột không?
is_conflict_error() {
    case "$1" in
        *UPDATE_INCOMPATIBLE*|*DUPLICATE_PACKAGE*|*ALREADY_EXISTS*|*VERSION_DOWNGRADE*)
            return 0 ;;
        *signatures*do*not*match*)
            return 0 ;;
    esac
    return 1
}

# Package name Android hợp lệ. Giá trị này đi vào "adb shell ..." (shell trên
# thiết bị parse lại chuỗi), nên mọi package name (từ manifest.json, aapt, tên
# OBB, thông báo lỗi, người dùng nhập) phải qua đây trước khi dùng với adb.
is_valid_package_name() {
    local re='^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$'
    [[ "$1" =~ $re ]]
}

# Như trên nhưng in lỗi; dùng: require_valid_package_name "$pkg" || return 1
require_valid_package_name() {
    if ! is_valid_package_name "$1"; then
        echo "   ❌ Package name không hợp lệ: '$1' — dừng lại, không chạy lệnh adb." >&2
        return 1
    fi
    return 0
}

uninstall_pkg() {
    local pkg="$1" out
    if ! is_valid_package_name "$pkg"; then
        UNINSTALL_ERROR="Package name không hợp lệ: '$pkg'"
        return 1
    fi
    out=$("$ADB" -s "$DEVICE" uninstall "$pkg" 2>&1 | tr -d '\r')
    echo "$out" | grep -q "Success" && return 0

    # App hệ thống hoặc cài cho user khác
    out=$("$ADB" -s "$DEVICE" shell pm uninstall --user 0 "$pkg" </dev/null 2>&1 | tr -d '\r')
    echo "$out" | grep -q "Success" && return 0

    UNINSTALL_ERROR="$out"
    return 1
}

confirm_uninstall() {
    local pkg="$1" ans
    [ "$AUTO_UNINSTALL" = "1" ] && return 0
    echo "   ⚠️  Gỡ $pkg sẽ XOÁ dữ liệu app (cài đặt, tài khoản, bản đồ offline)."
    if ! read -r -p "   Gỡ bản cũ rồi cài lại? [Y=có / n=không / a=luôn luôn]: " ans; then
        echo "   ⏭  Bỏ qua."
        return 1
    fi
    case "$ans" in
        n|N) echo "   ⏭  Bỏ qua."; return 1 ;;
        a|A) AUTO_UNINSTALL=1; return 0 ;;
        *)   return 0 ;;
    esac
}

# Khi cài fail do xung đột: gỡ bản cũ rồi cài lại.
# $1 = package name gợi ý (có thể rỗng), $2... = tham số cho run_install
retry_after_uninstall() {
    local pkg_hint="$1"; shift
    local cmd="$1"; shift

    is_conflict_error "$LAST_INSTALL_ERROR" || return 1

    local pkg="$pkg_hint"
    [ -z "$pkg" ] && pkg=$(pkg_from_error "$LAST_INSTALL_ERROR")
    if [ -z "$pkg" ]; then
        echo "   ❌ Không xác định được package name để gỡ (thiếu aapt)."
        echo "      Gỡ thủ công bằng chức năng 2 rồi cài lại."
        return 1
    fi

    require_valid_package_name "$pkg" || return 1
    confirm_uninstall "$pkg" || return 1

    echo -n "   🗑️  Gỡ bản cũ $pkg ... "
    if ! uninstall_pkg "$pkg"; then
        echo "❌"
        echo "$UNINSTALL_ERROR" | sed 's/^/      /'
        return 1
    fi
    echo "✅"

    echo -n "   🔄 Cài lại ... "
    if run_install "$cmd" "$@"; then
        echo "✅"
        return 0
    fi
    echo "❌"
    explain_error "$LAST_INSTALL_ERROR"
    return 1
}

# Gợi ý cách xử lý dựa trên mã lỗi của PackageManager
explain_error() {
    local err="$1"
    echo "   ↳ Lỗi từ thiết bị:"
    echo "$err" | sed 's/^/      /'
    case "$err" in
        *UPDATE_INCOMPATIBLE*|*DUPLICATE_PACKAGE*|*signatures*do*not*match*)
            echo "   💡 App đã cài sẵn nhưng khác chữ ký (bản mod vs bản gốc)." ;;
        *NO_MATCHING_ABIS*)
            echo "   💡 APK không có thư viện native cho ABI của xe (${DEVICE_ABILIST:-?})."
            echo "      Cần bản APK đúng kiến trúc." ;;
        *INSUFFICIENT_STORAGE*)
            echo "   💡 Hết dung lượng. Xoá bớt app hoặc dọn cache." ;;
        *OLDER_SDK*)
            echo "   💡 App yêu cầu Android mới hơn phiên bản trên xe." ;;
        *VERSION_DOWNGRADE*)
            echo "   💡 Bản đang cài mới hơn. Gỡ bản cũ rồi cài lại." ;;
        *INVALID_APK*|*PARSE*)
            echo "   💡 File APK hỏng hoặc không hợp lệ." ;;
        *TEST_ONLY*)
            echo "   💡 APK là bản test-only, cần cài kèm cờ -t." ;;
    esac
}

# Bật Developer settings, freeform, resize và cho phép app non-resizable vào multi-window.
enable_dev_freeform_resize() {
    ensure_device || return 1

    echo "🖥️  Đang bật 4 tùy chỉnh hệ thống..."
    local setting actual failed=0
    for setting in \
        development_settings_enabled \
        enable_freeform_support \
        force_resizable_activities \
        enable_non_resizable_multi_window; do
        echo -n "   ⚙️  $setting ... "
        if "$ADB" -s "$DEVICE" shell settings put global "$setting" 1 </dev/null \
           && actual=$("$ADB" -s "$DEVICE" shell settings get global "$setting" </dev/null \
                       | tr -d '\r' | tr -d '[:space:]') \
           && [ "$actual" = "1" ]; then
            echo "✅"
        else
            echo "❌ (giá trị đọc lại: ${actual:-?})"
            failed=1
        fi
    done

    if [ "$failed" -ne 0 ]; then
        echo "❌ Không bật được đầy đủ các tùy chỉnh; không khởi động lại thiết bị."
        return 1
    fi

    echo "🔄 Đang khởi động lại thiết bị để áp dụng..."
    if "$ADB" -s "$DEVICE" reboot </dev/null; then
        echo "✅ Đã bật 4 tùy chỉnh. Dùng chức năng 4 để kết nối lại sau khi thiết bị khởi động xong."
        DEVICE=""
        DEVICE_ABILIST=""
    else
        echo "❌ Không thể khởi động lại thiết bị."
        return 1
    fi
}

# Hàm cài 1 APK
install_apk() {
    local file="$1"
    echo -n "📦 $(basename "$file") ... "
    if run_install install "$file"; then
        echo "✅"
        return 0
    fi
    echo "❌"
    explain_error "$LAST_INSTALL_ERROR"

    retry_after_uninstall "$(get_package_name "$file")" install "$file"
}

# Trả về token config của 1 split (rỗng nếu là base APK)
split_token() {
    local name
    name=$(basename "$1")
    name="${name%.apk}"
    case "$name" in
        *config.*) echo "${name##*config.}" ;;
        *) echo "" ;;
    esac
}

is_abi_token() {
    local tok="$1" a
    for a in $KNOWN_ABIS; do
        [ "$tok" = "$a" ] && return 0
    done
    return 1
}

# Hàm cài XAPK/APKS/APKM (Split APKs + OBB)
install_xapk() {
    local xapk_file="$1"
    local temp_dir
    temp_dir=$(mktemp -d) || return 1
    trap 'rm -rf "$temp_dir"' RETURN

    echo "📦 XAPK: $(basename "$xapk_file")"

    echo -n "   📂 Giải nén... "
    local unzip_out
    if ! unzip_out=$(unzip -q -o "$xapk_file" -d "$temp_dir" 2>&1); then
        echo "❌"
        echo "$unzip_out" | tail -n 3 | sed 's/^/      /'
        return 1
    fi
    echo "✅"

    # Gói độc có thể chứa SYMLINK (unzip tạo lại link): `adb push`/`install` đi theo link và đẩy
    # file của máy host (vd ~/.ssh/id_rsa) lên /sdcard. Có link bất kỳ => từ chối cả gói.
    if [ -n "$(find "$temp_dir" -type l -print -quit)" ]; then
        echo "   ❌ Gói chứa symlink — từ chối cài (nghi gói độc hại)."
        return 1
    fi

    # Thu thập toàn bộ apk trong gói
    local all_apks=()
    while IFS= read -r apk; do
        [ -n "$apk" ] && all_apks+=("$apk")
    done < <(find "$temp_dir" -maxdepth 3 -type f -name "*.apk" | sort)

    if [ ${#all_apks[@]} -eq 0 ]; then
        echo "   ❌ Không tìm thấy file APK nào trong gói."
        return 1
    fi

    # --- Lọc ABI splits: chỉ giữ ĐÚNG 1 ABI khớp thiết bị ---
    # Cài nhiều ABI cùng lúc khiến PackageManager chọn sai native libs -> app crash.
    local abilist="$DEVICE_ABILIST"
    if [ -z "$abilist" ]; then
        abilist="arm64-v8a,armeabi-v7a"
        echo "   ⚠️  Không đọc được ABI của xe, mặc định: $abilist"
    fi

    local available_abis="" f tok
    for f in "${all_apks[@]}"; do
        tok=$(split_token "$f")
        if [ -n "$tok" ] && is_abi_token "$tok"; then
            available_abis="$available_abis $tok"
        fi
    done

    local chosen_abi=""
    if [ -n "$available_abis" ]; then
        local abi abi_tok
        for abi in $(echo "$abilist" | tr ',' ' '); do
            abi_tok=$(echo "$abi" | tr '-' '_')
            case " $available_abis " in
                *" $abi_tok "*) chosen_abi="$abi_tok"; break ;;
            esac
        done
        if [ -z "$chosen_abi" ]; then
            echo "   ❌ Gói không có ABI nào khớp với xe ($abilist)."
            echo "      ABI có trong gói:$available_abis"
            return 1
        fi
    fi

    # --- Chọn danh sách cài: base trước, rồi splits hợp lệ ---
    local base_apks=() split_apks=() skipped=0
    for f in "${all_apks[@]}"; do
        tok=$(split_token "$f")
        if [ -z "$tok" ]; then
            base_apks+=("$f")
        elif is_abi_token "$tok"; then
            if [ "$tok" = "$chosen_abi" ]; then
                split_apks+=("$f")
            else
                skipped=$((skipped + 1))
            fi
        else
            # split ngôn ngữ / mật độ màn hình: giữ lại, hệ thống tự chọn
            split_apks+=("$f")
        fi
    done

    if [ ${#base_apks[@]} -eq 0 ]; then
        echo "   ❌ Không tìm thấy base APK trong gói."
        return 1
    fi

    local install_list=("${base_apks[@]}" "${split_apks[@]}")
    if [ -n "$chosen_abi" ]; then
        echo "   🎯 ABI chọn: $chosen_abi (bỏ qua $skipped ABI splits không khớp)"
    fi

    # Package name: ưu tiên manifest.json của XAPK, sau đó đọc từ base APK
    local pkg=""
    if [ -f "$temp_dir/manifest.json" ]; then
        pkg=$(sed -n 's/.*"package_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
              "$temp_dir/manifest.json" | head -n 1)
    fi
    [ -z "$pkg" ] && pkg=$(get_package_name "${base_apks[0]}")
    if [ -n "$pkg" ]; then
        require_valid_package_name "$pkg" || return 1
    fi

    echo -n "   🚀 Đang cài đặt (${#install_list[@]} APKs)... "
    if run_install install-multiple "${install_list[@]}"; then
        echo "✅"
    else
        echo "❌"
        explain_error "$LAST_INSTALL_ERROR"
        retry_after_uninstall "$pkg" install-multiple "${install_list[@]}" || return 1
    fi

    # --- Push OBB nếu có (thiếu OBB là nguyên nhân app cài xong bị crash) ---
    local obb_files=()
    while IFS= read -r obb; do
        [ -n "$obb" ] && obb_files+=("$obb")
    done < <(find "$temp_dir" -type f -name "*.obb")

    if [ ${#obb_files[@]} -gt 0 ]; then
        local obb obb_pkg base_name
        for obb in "${obb_files[@]}"; do
            base_name=$(basename "$obb")
            obb_pkg="$pkg"
            if [ -z "$obb_pkg" ]; then
                # main.<version>.<package>.obb
                obb_pkg=$(echo "${base_name%.obb}" | sed -E 's/^(main|patch)\.[0-9]+\.//')
            fi
            require_valid_package_name "$obb_pkg" || return 1
            echo -n "   📁 OBB $base_name -> $obb_pkg ... "
            "$ADB" -s "$DEVICE" shell mkdir -p "/sdcard/Android/obb/$obb_pkg" </dev/null >/dev/null 2>&1
            local push_out
            if push_out=$("$ADB" -s "$DEVICE" push "$obb" \
                          "/sdcard/Android/obb/$obb_pkg/$base_name" 2>&1); then
                echo "✅"
            else
                echo "❌"
                echo "$push_out" | tail -n 3 | sed 's/^/      /'
                return 1
            fi
        done
    fi

    return 0
}

# Kết nối lần đầu
DEVICE=""
DEVICE_ABILIST=""
if ! connect_device; then
    exit 1
fi

# Vòng lặp chính
while true; do
    echo ""
    echo "=============================="
    echo "  Thiết bị: ${DEVICE:-<chưa kết nối>}"
    echo "=============================="
    echo "  1. Cài đặt APK / XAPK"
    echo "  2. Gỡ cài đặt APK"
    echo "  3. Liệt kê package"
    echo "  4. Kết nối lại thiết bị"
    if [ "$AUTO_UNINSTALL" = "1" ]; then
        echo "  5. Tự gỡ bản cũ khi xung đột: BẬT (không hỏi lại)"
    else
        echo "  5. Tự gỡ bản cũ khi xung đột: TẮT (sẽ hỏi trước)"
    fi
    echo "  6. Bật Developer settings / Freeform / Split app"
    echo "  7. Thoát"
    echo "=============================="
    # Hết stdin (EOF / Ctrl+D) thì thoát, tránh lặp menu vô hạn
    if ! read -r -p "Chọn chức năng (1-7): " CHOICE; then
        echo ""
        echo "👋 Thoát."
        exit 0
    fi

    case $CHOICE in
        1)
            ensure_device || continue
            echo ""
            echo "📦 Chế độ CÀI ĐẶT"
            echo "Kéo-thả file vào đây hoặc nhập đường dẫn:"
            echo "Hỗ trợ: .apk  .xapk  .apks  .apkm"
            read -r -p "> " INPUT_RAW

            if [ -z "$INPUT_RAW" ]; then
                echo "❌ Bạn chưa nhập file nào."
                continue
            fi

            # Làm sạch dấu ngoặc kép / khoảng trắng thừa ở hai đầu
            CLEAN_INPUT=$(echo "$INPUT_RAW" | sed -e 's/^["'\'' ]*//' -e 's/["'\'' ]*$//')

            SUCCESS=0
            FAIL=0
            START_TIME=$(date +%s)

            # Tách danh sách file mà KHÔNG dùng eval (eval cho phép chạy lệnh
            # tuỳ ý nếu tên file chứa $, ` hoặc ;). xargs bóc quote/backslash
            # của Terminal khi kéo-thả mà không thực thi gì.
            FILES=()
            if [ -f "$CLEAN_INPUT" ]; then
                FILES=("$CLEAN_INPUT")
            else
                while IFS= read -r f; do
                    [ -n "$f" ] && FILES+=("$f")
                done < <(printf '%s' "$INPUT_RAW" | xargs -n1 printf '%s\n' 2>/dev/null)
            fi

            if [ ${#FILES[@]} -eq 0 ]; then
                echo "❌ Không phân tích được đường dẫn: $INPUT_RAW"
                continue
            fi

            for file in "${FILES[@]}"; do
                if [ ! -f "$file" ]; then
                    echo "❌ Không tìm thấy: $file"
                    FAIL=$((FAIL + 1))
                    continue
                fi

                ext="${file##*.}"
                ext_lower=$(echo "$ext" | tr '[:upper:]' '[:lower:]')

                case $ext_lower in
                    apk)
                        if install_apk "$file"; then
                            SUCCESS=$((SUCCESS + 1))
                        else
                            FAIL=$((FAIL + 1))
                        fi
                        ;;
                    xapk|apks|apkm)
                        if install_xapk "$file"; then
                            SUCCESS=$((SUCCESS + 1))
                        else
                            FAIL=$((FAIL + 1))
                        fi
                        ;;
                    *)
                        echo "❌ Không hỗ trợ định dạng: $file"
                        FAIL=$((FAIL + 1))
                        ;;
                esac
            done

            END_TIME=$(date +%s)
            ELAPSED=$((END_TIME - START_TIME))

            echo ""
            echo "=============================="
            echo "✅ Thành công: $SUCCESS"
            echo "❌ Thất bại  : $FAIL"
            echo "⏱  Thời gian : ${ELAPSED}s"
            echo "=============================="
            ;;
        2)
            ensure_device || continue
            echo ""
            echo "🗑️  Chế độ GỠ CÀI ĐẶT"
            read -r -p "Nhập package name (ví dụ: vn.vietmap.live): " PACKAGE

            if [ -z "$PACKAGE" ]; then
                echo "❌ Bạn chưa nhập package name."
                continue
            fi
            require_valid_package_name "$PACKAGE" || continue

            echo "🗑️  Đang gỡ $PACKAGE ..."
            UNINSTALL_OUT=$("$ADB" -s "$DEVICE" uninstall "$PACKAGE" 2>&1)

            if echo "$UNINSTALL_OUT" | grep -q "^Success"; then
                echo "✅ Gỡ thành công!"
            else
                echo "❌ Gỡ thất bại."
                echo "$UNINSTALL_OUT" | sed 's/^/   /'
                case "$UNINSTALL_OUT" in
                    *DELETE_FAILED_INTERNAL_ERROR*|*not*installed*)
                        echo "   💡 Package chưa được cài, hoặc là app hệ thống."
                        echo "      Thử: adb shell pm uninstall --user 0 $PACKAGE" ;;
                esac
            fi
            ;;
        3)
            ensure_device || continue
            echo ""
            echo "📋 Danh sách package:"
            "$ADB" -s "$DEVICE" shell pm list packages </dev/null | tr -d '\r' | sort
            ;;
        4)
            echo ""
            connect_device
            ;;
        6)
            enable_dev_freeform_resize
            ;;
        5)
            if [ "$AUTO_UNINSTALL" = "1" ]; then
                AUTO_UNINSTALL=0
                echo "🔕 Đã TẮT: sẽ hỏi trước khi gỡ bản cũ."
            else
                AUTO_UNINSTALL=1
                echo "🔔 Đã BẬT: tự gỡ bản cũ khi xung đột, KHÔNG hỏi lại."
                echo "   ⚠️  Dữ liệu app cũ sẽ bị xoá."
            fi
            ;;
        7)
            echo "👋 Thoát."
            exit 0
            ;;
        *)
            echo "❌ Lựa chọn không hợp lệ."
            ;;
    esac
done
