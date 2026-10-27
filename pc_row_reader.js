/* Read public BookMyShow rows through this PC's ordinary Chrome connection. */
'use strict';
const {chromium}=require('playwright');

function seatUrl(show){
  const {event,venue_code:venue,session,date}=show;
  if(!/^ET[A-Z0-9]{6,}$/.test(event)||!/^[A-Z0-9]+$/.test(venue)||
     !/^[A-Za-z0-9]+$/.test(session)||!/^\d{8}$/.test(date))throw Error('Invalid showtime identity');
  return `https://in.bookmyshow.com/movies/hyderabad/seat-layout/${event}/${venue}/${session}/${date}`;
}

async function read(show){
  const url=seatUrl(show);
  const browser=await chromium.launch({
    executablePath:process.env.BMS_CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless:false,
    args:['--window-position=-32000,-32000','--window-size=800,600']
  });
  try{
    const page=await browser.newPage();
    const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:25000});
    if(!response||response.status()!==200)throw Error(`Seat page unavailable (HTTP ${response?.status()||0})`);
    if(page.url().split('?')[0]!==url)throw Error('Seat page redirected');
    const access=page.getByRole('button',{name:'Open accessibility seat selection',exact:true});
    await access.waitFor({state:'visible',timeout:12000});
    const sheet=page.locator('#bottomSheet-model-close');
    if(await sheet.isVisible())await sheet.click({timeout:3000});
    const selectSeats=page.getByRole('button',{name:'Select Seats',exact:true});
    if(await selectSeats.isVisible())await selectSeats.click({timeout:3000});
    await access.click({timeout:8000});
    const tickets=page.getByRole('combobox',{name:'Select number of tickets, required',exact:true});
    await tickets.selectOption({label:'1 Ticket'});
    const category=page.getByRole('combobox',{name:'Select seat category',exact:true});
    const rowSelect=page.getByRole('combobox',{name:'Select row',exact:true});
    const categories=await category.locator('option').evaluateAll(es=>es.filter(e=>e.value&&!e.disabled).map(e=>({value:e.value,label:e.textContent})));
    if(!categories.length)throw Error('No readable categories');
    const known=new Set(Object.values(show.categories||{}).map(c=>c.name));
    const rows=[],available=[];
    for(const cat of categories){
      const name=cat.label.split(/\s+-\s+₹/)[0].trim();
      if(!known.has(name))throw Error('Seat category identity mismatch');
      await category.selectOption(cat.value);
      const choices=await rowSelect.locator('option').evaluateAll(es=>es.filter(e=>e.value&&!e.disabled).map(e=>({value:e.value,label:e.textContent})));
      for(const row of choices){
        await rowSelect.selectOption(row.value);
        const grid=page.getByRole('grid',{name:'Seats for Row '+row.value,exact:true});
        await grid.waitFor({state:'visible',timeout:5000});
        const cells=await grid.locator('tbody tr').evaluateAll(es=>es.map(e=>[...e.querySelectorAll('td')].map(c=>c.textContent.trim())));
        if(!cells.length)throw Error('Empty row data');
        const label=row.label.replace(name+' Row ','').trim();
        const id=name+' / '+label;
        rows.push({id,category:name,row:label});
        for(const cell of cells){
          if(cell.length<2||!['Available','Available Best Seat','Booked','Booked Best Seat','Unavailable','Handicapped'].includes(cell[1]))throw Error('Unknown seat status');
          if(cell[1].startsWith('Available'))available.push({row_id:id,seat:cell[0]});
        }
      }
    }
    return {rows,available,url};
  }finally{await browser.close()}
}

async function main(){
  let input='';for await(const part of process.stdin)input+=part;
  try{process.stdout.write(JSON.stringify({data:await read(JSON.parse(input))})+'\n')}
  catch(error){process.stdout.write(JSON.stringify({error:String(error.message||error).split('\n')[0].slice(0,200)})+'\n');process.exitCode=1}
}
if(require.main===module)main();
module.exports={read,seatUrl};
