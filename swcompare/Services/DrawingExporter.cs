using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace SWCompare.Services;

/// <summary>
/// Exports a .SLDDRW document to PDF on disk so the Python pipeline can run
/// its existing PDF vision pass on the rendered drawing sheets.
/// </summary>
public sealed class DrawingExporter
{
    public byte[] ExportToPdfBytes(IModelDoc2 drawingDoc)
    {
        var tmp = Path.Combine(Path.GetTempPath(), $"swcompare-{Guid.NewGuid():N}.pdf");
        try
        {
            int errors = 0, warnings = 0;
            var ok = drawingDoc.Extension.SaveAs(
                tmp,
                (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                (int)swSaveAsOptions_e.swSaveAsOptions_Silent,
                null, null,
                ref errors, ref warnings);
            if (!ok)
                throw new IOException($"SaveAs PDF failed (errors=0x{errors:X} warnings=0x{warnings:X}).");
            return File.ReadAllBytes(tmp);
        }
        finally
        {
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { /* best-effort */ }
        }
    }
}
