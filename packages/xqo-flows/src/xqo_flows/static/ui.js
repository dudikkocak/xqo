/* flows plugin UI in the dashboard: the "flows" header link comes from the plugin's nav; this script shows a
   toast when a flow run waits for your approval or ends, with a link to the run in the flow editor. */
(function(){
  const open=(flow,run)=>`/plugins/flows/editor.html#${encodeURIComponent(flow)}${run?"/"+encodeURIComponent(run):""}`;
  XQO.on(e=>{
    if(e.kind==="flow_node"&&e.meta&&e.meta.status==="waiting"){
      toast(`Flow ${e.meta.flow}: waiting for your approval`,null,{label:"Open",run:()=>{location.href=open(e.meta.flow,e.meta.run_id)}});
    }else if(e.kind==="flow_run"&&e.meta&&["done","error","cancelled"].includes(e.meta.status)){
      toast(`Flow ${e.meta.flow}: ${e.meta.status}`,null,{label:"Open",run:()=>{location.href=open(e.meta.flow,e.meta.run_id)}});
    }
  });
})();
