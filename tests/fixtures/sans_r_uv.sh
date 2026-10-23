#!/bin/sh
# Public resolver fixture: failures and causal interruption checkpoints.
root=$(/usr/bin/dirname "$0")
replace_output() {
    /bin/rm "$1"
    /bin/ln -s "$root/unrelated" "$1"
}
checkpoint() {
    trap 'echo "$message" >&2; exit 1' INT
    exec 3>"$root/alive"
    printf 1 >&3
    /bin/cat "$root/wait" >/dev/null &
    waiter=$!
    printf 1 > "$root/started"
    wait "$waiter"
    exit 85
}
if [ "$(/usr/bin/basename "$0")" = invalid-python ]; then
    [ "$#" = 3 ] && exit 0
    if [ "$(/bin/cat "$root/mode")" = inspection-interrupt ]; then
        message="fixture Python inspection interrupted"
        checkpoint
    fi
    /bin/cat "$root/invalid-inspection.json" > "$4"
    [ "$(/bin/cat "$root/mode")" != replace-inspection ] || replace_output "$4"
    exit 0
fi
printf '%s\n' "$@" >> "$root/resolutions.log"
needs_preparation=false
for output do
    [ "$output" = py-yaml12 ] && needs_preparation=true
done
if $needs_preparation; then
    case $(/bin/cat "$root/mode") in
        failure) echo "fixture Python resolution failed" >&2; exit 1 ;;
        replace-output|replace-inspection)
            printf '%s' "$root/invalid-python" > "$output"
            case $(/bin/cat "$root/mode") in
                replace-output) replace_output "$output" ;;
            esac
            exit 0 ;;
        inspection|inspection-interrupt) printf '%s' "$root/invalid-python" > "$output"; exit 0 ;;
        interrupt) message="fixture Python resolution interrupted"; checkpoint ;;
        activation-failure)
            "$root/real-uv" "$@" || exit $?
            for output do :; done
            python=$(/bin/cat "$output")
            printf 'raise RuntimeError("synthetic activation failure")\n' > "$(/usr/bin/dirname "$python")/activate_this.py"
            exit 0 ;;
    esac
fi
exec "$root/real-uv" "$@"
