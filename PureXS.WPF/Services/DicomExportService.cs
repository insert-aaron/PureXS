using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using FellowOakDicom;
using FellowOakDicom.IO.Buffer;

namespace PureXS.Services;

public class DicomExportService : IDicomExportService
{
    public async Task<string?> ExportAsync(
        string patientName,
        string patientId,
        string patientDob,
        string examType,
        byte[] imageBytes,
        double kvPeak,
        string outputDirectory,
        string filePrefix,
        CancellationToken ct = default)
    {
        try
        {
            // Decode PNG to grayscale pixel data using WPF imaging
            var (pixelData, width, height) = DecodeToGrayscale(imageBytes);

            var dataset = new DicomDataset();

            // --- SOP Common ---
            var sopClassUid = new DicomUID("1.2.840.10008.5.1.4.1.1.1.1", "Digital X-Ray Image Storage - For Presentation", DicomUidType.SOPClass);
            var sopInstanceUid = DicomUIDGenerator.GenerateDerivedFromUUID();
            dataset.Add(DicomTag.SOPClassUID, sopClassUid);
            dataset.Add(DicomTag.SOPInstanceUID, sopInstanceUid);
            dataset.Add(DicomTag.SpecificCharacterSet, "ISO_IR 100");

            // --- Patient Module ---
            dataset.Add(DicomTag.PatientName, patientName);
            dataset.Add(DicomTag.PatientID, patientId);
            dataset.Add(DicomTag.PatientBirthDate, patientDob);

            // --- Study Module ---
            var now = DateTime.Now;
            var studyInstanceUid = DicomUIDGenerator.GenerateDerivedFromUUID();
            dataset.Add(DicomTag.StudyDate, now.ToString("yyyyMMdd"));
            dataset.Add(DicomTag.StudyTime, now.ToString("HHmmss"));
            dataset.Add(DicomTag.StudyInstanceUID, studyInstanceUid);
            dataset.Add(DicomTag.AccessionNumber, "");
            dataset.Add(DicomTag.StudyID, "1");

            // --- Series Module ---
            var isCeph = examType.StartsWith("Ceph", StringComparison.OrdinalIgnoreCase);
            var seriesInstanceUid = DicomUIDGenerator.GenerateDerivedFromUUID();
            dataset.Add(DicomTag.Modality, isCeph ? "DX" : "PX");
            dataset.Add(DicomTag.SeriesInstanceUID, seriesInstanceUid);
            dataset.Add(DicomTag.SeriesNumber, 1);
            dataset.Add(DicomTag.SeriesDescription, examType);

            // --- Image Module ---
            dataset.Add(DicomTag.InstanceNumber, 1);

            // --- Acquisition ---
            dataset.Add(DicomTag.KVP, (decimal)kvPeak);
            dataset.Add(DicomTag.Manufacturer, "Dentsply Sirona");
            dataset.Add(DicomTag.ManufacturerModelName, "Orthophos XG");
            dataset.Add(DicomTag.SoftwareVersions, "PureXS 1.0");
            dataset.Add(DicomTag.BodyPartExamined, isCeph ? "SKULL" : "JAW");
            var viewPosition = examType switch
            {
                "Ceph Lateral" => "LAT",
                "Ceph Frontal" => "AP",
                _ => "PA"
            };
            dataset.Add(DicomTag.ViewPosition, viewPosition);

            // --- Pixel Data ---
            dataset.Add(DicomTag.Rows, (ushort)height);
            dataset.Add(DicomTag.Columns, (ushort)width);
            dataset.Add(DicomTag.BitsAllocated, (ushort)8);
            dataset.Add(DicomTag.BitsStored, (ushort)8);
            dataset.Add(DicomTag.HighBit, (ushort)7);
            dataset.Add(DicomTag.SamplesPerPixel, (ushort)1);
            dataset.Add(DicomTag.PhotometricInterpretation, "MONOCHROME2");
            dataset.Add(DicomTag.PixelRepresentation, (ushort)0);
            dataset.Add(DicomTag.WindowCenter, "128");
            dataset.Add(DicomTag.WindowWidth, "256");

            var buffer = new MemoryByteBuffer(pixelData);
            dataset.AddOrUpdate(new DicomOtherByte(DicomTag.PixelData, buffer));

            // --- Transfer Syntax ---
            var file = new DicomFile(dataset);
            file.FileMetaInfo.TransferSyntax = DicomTransferSyntax.ExplicitVRLittleEndian;
            file.FileMetaInfo.MediaStorageSOPClassUID = sopClassUid;
            file.FileMetaInfo.MediaStorageSOPInstanceUID = sopInstanceUid;

            // Ensure output directory exists
            Directory.CreateDirectory(outputDirectory);

            var outputPath = Path.Combine(outputDirectory, $"{filePrefix}.dcm");
            await file.SaveAsync(outputPath);

            return outputPath;
        }
        catch
        {
            return null;
        }
    }

    private static (byte[] pixelData, int width, int height) DecodeToGrayscale(byte[] pngBytes)
    {
        var bitmapImage = new BitmapImage();
        using (var ms = new MemoryStream(pngBytes))
        {
            bitmapImage.BeginInit();
            bitmapImage.CacheOption = BitmapCacheOption.OnLoad;
            bitmapImage.StreamSource = ms;
            bitmapImage.EndInit();
            bitmapImage.Freeze();
        }

        // Convert to Gray8
        var gray = new FormatConvertedBitmap(bitmapImage, PixelFormats.Gray8, null, 0);
        gray.Freeze();

        int width = gray.PixelWidth;
        int height = gray.PixelHeight;
        int stride = width; // 1 byte per pixel for Gray8
        var pixels = new byte[height * stride];
        gray.CopyPixels(pixels, stride, 0);

        return (pixels, width, height);
    }
}
