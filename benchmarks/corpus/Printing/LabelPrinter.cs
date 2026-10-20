using System.Runtime.InteropServices;

namespace Acme.Mes.Printing
{
    public class LabelPrinter
    {
        [DllImport("tsclib.dll")] private static extern int openport(string printerName);
        [DllImport("tsclib.dll")] private static extern int sendcommand(string command);
        [DllImport("tsclib.dll")] private static extern int closeport();

        public void PrintLotLabel(string printerName, string lotNo)
        {
            openport(printerName);
            sendcommand("SIZE 60 mm, 30 mm");
            sendcommand("CLS");
            sendcommand("BARCODE 40,40,\"39\",60,1,0,2,4,\"" + lotNo + "\"");
            sendcommand("PRINT 1");
            closeport();
        }
    }
}
