using System.Web.Http;
using Acme.Mes.Models;
using Acme.Mes.Services;

namespace Acme.Mes.Controllers
{
    [RoutePrefix("api/scrap")]
    public class ScrapController : ApiController
    {
        private readonly IScrapService _scrapService;

        public ScrapController(IScrapService scrapService)
        {
            _scrapService = scrapService;
        }

        [HttpPost, Route("")]
        public IHttpActionResult Create(ScrapRecord record)
        {
            _scrapService.Save(record);
            return Ok();
        }

        [HttpGet, Route("lot/{lotNo}")]
        public IHttpActionResult ByLot(string lotNo)
        {
            return Ok(_scrapService.GetByLot(lotNo));
        }
    }
}
