#Requires -RunAsAdministrator
param([Parameter(Mandatory)][string] $CleanupScript)
$ErrorActionPreference = 'Stop'

# Exercise the public migration script with inert persistent WFP fixtures.
# Refuse existing Console state before mutation; never provision accounts.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public sealed class MigrationFixture : IDisposable {
    static Guid ConsoleProvider = new Guid("892dda22-3948-4753-9117-e5d1a6496f41");
    static Guid ConsoleSublayer = new Guid("414c5dc3-4d32-4c7c-95ef-621f0cf6431a");
    Guid OtherProvider = Guid.NewGuid(), OtherSublayer = Guid.NewGuid();
    Guid[] Keys = { Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid() };
    IntPtr engine;
    bool created;
    [StructLayout(LayoutKind.Sequential)]
    public struct Display {
        [MarshalAs(UnmanagedType.LPWStr)] public string name;
        [MarshalAs(UnmanagedType.LPWStr)] public string description;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct Blob { public uint size; public IntPtr data; }
    [StructLayout(LayoutKind.Sequential)]
    public struct Value { public uint type; public ulong value; }
    [StructLayout(LayoutKind.Sequential)]
    public struct Action { public uint type; public Guid key; }
    [StructLayout(LayoutKind.Sequential)]
    public struct Provider {
        public Guid key; public Display display; public uint flags;
        public Blob data; public IntPtr service;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct Sublayer {
        public Guid key; public Display display; public uint flags;
        public IntPtr provider; public Blob data; public ushort weight;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct Filter {
        public Guid key; public Display display; public uint flags;
        public IntPtr provider; public Blob data; public Guid layer, sublayer;
        public Value weight; public uint count; public IntPtr conditions;
        public Action action; public ulong contextLow, contextHigh;
        public IntPtr reserved; public ulong id; public Value effectiveWeight;
    }
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    static extern uint FwpmEngineOpen0(string server, uint authentication, IntPtr identity, IntPtr session, out IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmEngineClose0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionBegin0(IntPtr engine, uint flags);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionCommit0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmTransactionAbort0(IntPtr engine);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmProviderAdd0(IntPtr engine, ref Provider provider, IntPtr security);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmSubLayerAdd0(IntPtr engine, ref Sublayer sublayer, IntPtr security);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterAdd0(IntPtr engine, ref Filter filter, IntPtr security, out ulong id);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmProviderGetByKey0(IntPtr engine, ref Guid key, out IntPtr entry);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmSubLayerGetByKey0(IntPtr engine, ref Guid key, out IntPtr entry);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterGetByKey0(IntPtr engine, ref Guid key, out IntPtr entry);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmFilterDeleteByKey0(IntPtr engine, ref Guid key);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmSubLayerDeleteByKey0(IntPtr engine, ref Guid key);
    [DllImport("fwpuclnt.dll")] static extern uint FwpmProviderDeleteByKey0(IntPtr engine, ref Guid key);
    [DllImport("fwpuclnt.dll")] static extern void FwpmFreeMemory0(ref IntPtr memory);

    static void Check(uint status, uint missing = 0) {
        if (status != 0 && status != missing) throw new Exception("WFP fixture: 0x" + status.ToString("x8"));
    }
    delegate uint Get(IntPtr engine, ref Guid key, out IntPtr entry);
    void Expect(Get get, Guid key, uint missing, bool absent) {
        IntPtr entry;
        uint status = get(engine, ref key, out entry);
        if (status == 0) FwpmFreeMemory0(ref entry);
        if (status != (absent ? missing : 0)) throw new Exception("Unexpected WFP state for " + key + ": 0x" + status.ToString("x8"));
    }
    public MigrationFixture() {
        Check(FwpmEngineOpen0(null, 10, IntPtr.Zero, IntPtr.Zero, out engine));
    }
    public void Create() {
        // These checks must precede the transaction and all cleanup eligibility.
        Expect(FwpmProviderGetByKey0, ConsoleProvider, 0x80320005, true);
        Expect(FwpmSubLayerGetByKey0, ConsoleSublayer, 0x80320007, true);
        Check(FwpmTransactionBegin0(engine, 0));
        try {
            Add(ConsoleProvider, ConsoleSublayer, 0, 2);
            Add(OtherProvider, OtherSublayer, 2, 3);
            Check(FwpmTransactionCommit0(engine));
            created = true;
        } finally { if (!created) Check(FwpmTransactionAbort0(engine)); }
    }
    void Add(Guid providerKey, Guid sublayerKey, int first, int end) {
        var display = new Display { name = "Console migration acceptance fixture" };
        var provider = new Provider { key = providerKey, display = display, flags = 1 };
        Check(FwpmProviderAdd0(engine, ref provider, IntPtr.Zero));
        IntPtr pointer = Marshal.AllocHGlobal(16);
        try {
            Marshal.StructureToPtr(providerKey, pointer, false);
            var sublayer = new Sublayer { key = sublayerKey, display = display, flags = 1, provider = pointer };
            Check(FwpmSubLayerAdd0(engine, ref sublayer, IntPtr.Zero));
            for (int i = first; i < end; ++i) {
                var filter = new Filter {
                    key = Keys[i], display = display, flags = 1, provider = pointer,
                    layer = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82"), sublayer = sublayerKey,
                    // FWP_ACTION_CONTINUE: no allow/block decision or callout.
                    action = new Action { type = 8198 }
                };
                ulong id;
                Check(FwpmFilterAdd0(engine, ref filter, IntPtr.Zero, out id));
            }
        } finally { Marshal.FreeHGlobal(pointer); }
    }
    public void Verify(bool retired) {
        Expect(FwpmProviderGetByKey0, ConsoleProvider, 0x80320005, retired);
        Expect(FwpmSubLayerGetByKey0, ConsoleSublayer, 0x80320007, retired);
        for (int i = 0; i < 3; ++i) Expect(FwpmFilterGetByKey0, Keys[i], 0x80320003, i < 2 && retired);
        Expect(FwpmProviderGetByKey0, OtherProvider, 0x80320005, false);
        Expect(FwpmSubLayerGetByKey0, OtherSublayer, 0x80320007, false);
    }
    public void Dispose() {
        try {
            if (created) {
                foreach (var original in Keys) {
                    var key = original;
                    Check(FwpmFilterDeleteByKey0(engine, ref key), 0x80320003);
                }
                Check(FwpmSubLayerDeleteByKey0(engine, ref ConsoleSublayer), 0x80320007);
                Check(FwpmProviderDeleteByKey0(engine, ref ConsoleProvider), 0x80320005);
                Check(FwpmSubLayerDeleteByKey0(engine, ref OtherSublayer), 0x80320007);
                Check(FwpmProviderDeleteByKey0(engine, ref OtherProvider), 0x80320005);
            }
        } finally { Check(FwpmEngineClose0(engine)); }
    }
}
'@
$fixture = [MigrationFixture]::new()
try {
    $fixture.Create()
    & $CleanupScript -WhatIf
    $fixture.Verify($false)
    & $CleanupScript -Confirm:$false
    $fixture.Verify($true)
    & $CleanupScript -Confirm:$false
    $fixture.Verify($true)
} finally {
    $fixture.Dispose()
}
Write-Output 'Migration removed only Console WFP state; dry-run and repetition verified.'
