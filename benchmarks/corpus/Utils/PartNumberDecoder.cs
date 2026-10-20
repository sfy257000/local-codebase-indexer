namespace Acme.Mes.Utils
{
    public static class PartNumberDecoder
    {
        // 料號各碼位置：第 4 碼為層數，第 7 碼為表面處理
        public static int GetLayers(string partNbr)
        {
            return int.Parse(partNbr.Substring(3, 1));
        }

        public static char GetSurfaceFinish(string partNbr)
        {
            return partNbr[6];
        }
    }
}
