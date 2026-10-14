#Requires -RunAsAdministrator
<# One-time, operator-invoked migration only. See docs/WINDOWS.md.
Never called by launch or installation.
The fixed identities come from the pinned runner's product.rs and wfp.rs.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param()

$ErrorActionPreference = 'Stop'
if (-not ('ConsoleLegacyWfp' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class ConsoleLegacyWfp {
    // Console transforms only data1 of Codex's GUIDs by XOR with 0xa71c093e.
    static Guid Provider = new Guid("892dda22-3948-4753-9117-e5d1a6496f41");
    static Guid Sublayer = new Guid("414c5dc3-4d32-4c7c-95ef-621f0cf6431a");
    [StructLayout(LayoutKind.Sequential)]
    struct FilterPrefix {
        public Guid key;
        public IntPtr name, description;
        public uint flags;
        public IntPtr provider;
    }
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    static extern uint FwpmEngineOpen0(string server, uint authentication, IntPtr identity, IntPtr session, out IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmEngineClose0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionBegin0(IntPtr engine, uint flags);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionCommit0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionAbort0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterCreateEnumHandle0(IntPtr engine, IntPtr template, out IntPtr enumeration);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterDestroyEnumHandle0(IntPtr engine, IntPtr enumeration);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterEnum0(IntPtr engine, IntPtr enumeration, uint count, out IntPtr entries, out uint returned);
    [DllImport("fwpuclnt.dll")] static extern void FwpmFreeMemory0(ref IntPtr memory);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterDeleteByKey0(IntPtr engine, ref Guid key);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmSubLayerDeleteByKey0(IntPtr engine, ref Guid key);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmProviderDeleteByKey0(IntPtr engine, ref Guid key);

    static void Check(uint status, string operation, uint missing = 0) {
        if (status != 0 && status != missing)
            throw new InvalidOperationException(operation + ": 0x" + status.ToString("x8"));
    }
    static List<Guid> Filters(IntPtr engine) {
        IntPtr enumeration;
        Check(FwpmFilterCreateEnumHandle0(engine, IntPtr.Zero, out enumeration), "enumerate filters");
        var result = new List<Guid>();
        try {
            while (true) {
                IntPtr entries;
                uint count;
                Check(FwpmFilterEnum0(engine, enumeration, 128, out entries, out count), "read filters");
                try {
                    for (int i = 0; i < count; ++i) {
                        var entry = (FilterPrefix)Marshal.PtrToStructure(Marshal.ReadIntPtr(entries, i * IntPtr.Size), typeof(FilterPrefix));
                        if (entry.provider != IntPtr.Zero && (Guid)Marshal.PtrToStructure(entry.provider, typeof(Guid)) == Provider)
                            result.Add(entry.key);
                    }
                } finally { if (entries != IntPtr.Zero) FwpmFreeMemory0(ref entries); }
                if (count == 0) return result;
            }
        } finally { Check(FwpmFilterDestroyEnumHandle0(engine, enumeration), "close enumeration"); }
    }
    public static int Remove() {
        IntPtr engine;
        Check(FwpmEngineOpen0(null, 10, IntPtr.Zero, IntPtr.Zero, out engine), "open WFP");
        bool transaction = false;
        try {
            Check(FwpmTransactionBegin0(engine, 0), "begin cleanup");
            transaction = true;
            var filters = Filters(engine);
            foreach (var filter in filters) {
                var key = filter;
                Check(FwpmFilterDeleteByKey0(engine, ref key), "remove Console filter", 0x80320003);
            }
            Check(FwpmSubLayerDeleteByKey0(engine, ref Sublayer), "remove Console sublayer", 0x80320007);
            Check(FwpmProviderDeleteByKey0(engine, ref Provider), "remove Console provider", 0x80320005);
            Check(FwpmTransactionCommit0(engine), "commit cleanup");
            transaction = false;
            if (Filters(engine).Count != 0) throw new InvalidOperationException("Console filters remain");
            return filters.Count;
        } finally {
            try { if (transaction) Check(FwpmTransactionAbort0(engine), "abort cleanup"); }
            finally { Check(FwpmEngineClose0(engine), "close WFP"); }
        }
    }
}
'@
}
if ($PSCmdlet.ShouldProcess('legacy Console WFP provider 892dda22-3948-4753-9117-e5d1a6496f41', 'Remove its filters, sublayer, and provider')) {
    $removed = [ConsoleLegacyWfp]::Remove()
    Write-Output "Removed $removed legacy Console WFP filters; Console provider and sublayer are absent."
}
