"""Render a compact visual seat map for Telegram row alerts."""
from datetime import datetime
from io import BytesIO

from PIL import Image,ImageDraw,ImageFont


def render(show,data,highlight_rows=()):
    seats=data.get('seats') or []
    if not seats:return None
    row_order=[row['id'] for row in data.get('rows',[])]
    grouped={key:[] for key in row_order}
    for seat in seats:
        if seat.get('row_id') in grouped:grouped[seat['row_id']].append(seat)
    grouped={key:value for key,value in grouped.items() if value}
    if not grouped:return None
    max_count=max(map(len,grouped.values()))
    width=1100;label_width=140;right_pad=34;gap=4
    cell=min(22,max(9,(width-label_width-right_pad-gap*max_count)//max_count))
    cell_h=max(16,min(23,cell+1));row_gap=8
    title_height=142;footer_height=76
    height=title_height+len(grouped)*(cell_h+row_gap)+footer_height
    image=Image.new('RGB',(width,height),'#f8fafc');draw=ImageDraw.Draw(image)
    regular=ImageFont.load_default();bold=ImageFont.load_default()
    draw.rounded_rectangle((22,18,width-22,title_height-18),radius=16,fill='#ffffff',outline='#e2e8f0',width=2)
    draw.text((44,34),'BOOKMYSHOW · SEAT AVAILABILITY',font=bold,fill='#64748b')
    draw.text((44,58),str(show.get('movie','Movie')),font=bold,fill='#111827')
    try:date=datetime.strptime(show.get('date',''),'%Y%m%d').strftime('%d %b %Y')
    except (TypeError,ValueError):date=str(show.get('date') or '')
    draw.text((44,82),f"{show.get('venue','')}  ·  {date}  ·  {show.get('time','')}",font=regular,fill='#475569')
    draw.rounded_rectangle((44,106,61,123),radius=4,fill='#35a879')
    draw.text((70,107),'Available',font=regular,fill='#334155')
    draw.rounded_rectangle((164,106,181,123),radius=4,fill='#d8dee8',outline='#aeb8c8')
    draw.text((190,107),'Unavailable',font=regular,fill='#334155')
    highlighted=set(highlight_rows);y=title_height
    for row_id,row_seats in grouped.items():
        category,row=row_id.rsplit(' / ',1) if ' / ' in row_id else ('Seats',row_id)
        if row_id in highlighted:
            draw.rounded_rectangle((28,y-4,width-28,y+cell_h+4),radius=8,fill='#ecfdf5')
        draw.text((42,y+4),f'{category} · {row}',font=bold,fill='#0f172a')
        count=len(row_seats);start=label_width+max(0,(max_count-count)*(cell+gap)//2)
        for index,seat in enumerate(row_seats):
            x=start+index*(cell+gap);available=bool(seat.get('available'))
            fill='#35a879' if available else '#d8dee8'
            draw.rounded_rectangle((x,y,x+cell,y+cell_h),radius=3,fill=fill,
                outline='#229267' if available else '#aeb8c8',width=1)
            label=str(seat.get('seat',''))
            if cell>=13 and label:
                box=draw.textbbox((0,0),label,font=regular);tw=box[2]-box[0]
                if tw<cell:draw.text((x+(cell-tw)//2,y+4),label,font=regular,fill='#ffffff' if available else '#475569')
        y+=cell_h+row_gap
    draw.text((42,height-42),'Seat status can change before you open BookMyShow.',font=regular,fill='#64748b')
    output=BytesIO();image.save(output,format='PNG',optimize=True)
    return output.getvalue()
